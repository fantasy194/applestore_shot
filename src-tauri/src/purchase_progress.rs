//! Small presentation history derived only from observed purchase events.
use super::{terminal, Task};
use serde::{Deserialize, Serialize};
use serde_json::Value;

const PHASES: [&str; 5] = ["prepare", "verify", "checkout", "submit", "payment"];

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct PurchaseProgress {
    #[serde(default)]
    pub current: Option<String>,
    #[serde(default)]
    pub completed: Vec<String>,
}
impl PurchaseProgress {
    pub(super) fn sanitized(&self) -> Self {
        let current = self
            .current
            .as_deref()
            .filter(|phase| PHASES.contains(phase))
            .map(str::to_owned);
        let current_rank = current
            .as_deref()
            .and_then(|current| PHASES.iter().position(|phase| *phase == current));
        let completed = PHASES
            .iter()
            .enumerate()
            .filter(|(rank, phase)| {
                current_rank.is_some_and(|current| *rank < current)
                    && self.completed.iter().take(100).any(|seen| seen == **phase)
            })
            .map(|(_, phase)| (*phase).into())
            .collect();
        Self { current, completed }
    }
    fn observe(&mut self, phase: &str) {
        *self = self.sanitized();
        let next = PHASES
            .iter()
            .position(|candidate| *candidate == phase)
            .unwrap();
        if let Some(previous) = self
            .current
            .as_deref()
            .and_then(|phase| PHASES.iter().position(|candidate| *candidate == phase))
        {
            if next > previous && previous < 4 {
                let observed = PHASES[previous].to_owned();
                if !self.completed.contains(&observed) {
                    self.completed.push(observed);
                }
            }
        }
        self.completed.retain(|phase| {
            PHASES
                .iter()
                .position(|candidate| *candidate == phase)
                .is_some_and(|rank| rank < next)
        });
        self.completed
            .sort_by_key(|phase| PHASES.iter().position(|candidate| *candidate == phase));
        self.current = Some(phase.into());
    }
}

fn stage_phase(message: &str) -> Option<&'static str> {
    let text: String = message.chars().take(2000).collect();
    let text = text.trim();
    match text.to_ascii_lowercase().as_str() {
        "prepare" | "account" | "bag" => return Some("prepare"),
        "verify" | "search" | "fulfillment" | "pickup" => return Some("verify"),
        "checkout" | "review" | "billing" => return Some("checkout"),
        "submit" => return Some("submit"),
        _ => {}
    }
    // QR retrieval follows confirmed submission; its text is not a new purchase attempt.
    if text.contains("二维码") || text.contains("订单已创建") {
        return None;
    }
    if text.contains("正在提交订单") {
        Some("submit")
    } else if ["结算", "结账", "付款方式", "订单复核", "订单确认页"]
        .iter()
        .any(|token| text.contains(token))
    {
        Some("checkout")
    } else if [
        "自提门店",
        "自提时段",
        "取货联系人",
        "配送方式",
        "核对商品",
        "已核对商品",
        "商品、数量",
    ]
    .iter()
    .any(|token| text.contains(token))
    {
        Some("verify")
    } else if ["登录", "账户", "账号", "购物袋", "加购", "下单第", "准备"]
        .iter()
        .any(|token| text.contains(token))
    {
        Some("prepare")
    } else {
        None
    }
}

pub(super) fn update(task: &mut Task, event: &Value) {
    if !matches!(task.action.as_str(), "purchase" | "checkout")
        || terminal(&task.status)
        || event
            .get("account_id")
            .is_some_and(|account| account != &task.account_id)
        || event.get("task_id").is_some_and(|id| id != &task.id)
        || event["queue_job_id"].as_str().is_some_and(|id| {
            !task.queue_job_ids.is_empty() && !task.queue_job_ids.iter().any(|known| known == id)
        })
    {
        return;
    }
    match event["type"].as_str() {
        Some("stage") => {
            let failed = event["authentication_failed"] == true
                || event["message"].as_str().is_some_and(|message| {
                    let message: String = message.chars().take(2000).collect();
                    // A configured retry counter names failures without reporting a failed step.
                    let message = message.replace("失败重试", "重试");
                    [
                        "失败",
                        "未通过",
                        "仍要求登录",
                        "无法",
                        "未能",
                        "错误",
                        "异常",
                    ]
                    .iter()
                    .any(|failure| message.contains(failure))
                });
            if failed {
                return;
            }
            if let Some(phase) = event["message"].as_str().and_then(stage_phase) {
                task.purchase_progress
                    .get_or_insert_with(Default::default)
                    .observe(phase);
            }
        }
        Some("progress_result" | "queue_progress" | "queue_finished" | "result") => {
            let Some(result) = event.get("result").filter(|result| result.is_object()) else {
                return;
            };
            let attention = super::workspace_feed::purchase_attention(result);
            let unknown = attention["unknown_unidentified"] == true
                || attention["unknown_order_numbers"]
                    .as_array()
                    .is_some_and(|rows| !rows.is_empty());
            let confirmed = attention["pending_order_numbers"]
                .as_array()
                .is_some_and(|rows| !rows.is_empty());
            if unknown {
                if let Some(progress) = task
                    .purchase_progress
                    .as_mut()
                    .filter(|progress| progress.current.as_deref() == Some("payment"))
                {
                    progress.observe("submit");
                }
            } else if confirmed {
                let progress = task.purchase_progress.get_or_insert_with(Default::default);
                progress.observe("payment");
                if !progress.completed.iter().any(|phase| phase == "submit") {
                    progress.completed.push("submit".into());
                }
            }
        }
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn fixture_task() -> Task {
        serde_json::from_value(json!({"id":"task","account_id":"account","action":"purchase","status":"running",
            "stage":"执行中","created_at":0,"updated_at":0,"prompt":null,"result":null,"order_submitted":false,"logs":[]})).unwrap()
    }
    fn stage(task: &mut Task, message: &str) {
        super::super::apply_event(task, &json!({"type":"stage","message":message}));
    }
    fn progress(task: &Task) -> Value {
        serde_json::to_value(task.purchase_progress.as_ref().unwrap()).unwrap()
    }

    #[test]
    fn observed_phases_only_complete_when_advanced_and_survive_terminal_errors() {
        let mut task = fixture_task();
        assert!(task.purchase_progress.is_none());
        for message in ["prepare", "正在查询自提门店", "正在提交订单"] {
            stage(&mut task, message);
        }
        assert_eq!(
            progress(&task),
            json!({"current":"submit","completed":["prepare","verify"]})
        );
        super::super::apply_event(
            &mut task,
            &json!({"type":"error","message":"官网未接受提交，请核对库存"}),
        );
        assert_eq!(task.stage, "官网未接受提交，请核对库存");
        stage(&mut task, "prepare");
        assert_eq!(
            progress(&task),
            json!({"current":"submit","completed":["prepare","verify"]})
        );
    }

    #[test]
    fn authentication_failure_and_explicit_failure_stages_never_mark_prepare_complete() {
        for flagged in [true, false] {
            let mut task = fixture_task();
            stage(&mut task, "prepare");
            super::super::apply_event(
                &mut task,
                &json!({"type":"stage","message":"结账重新认证后仍要求登录","authentication_failed":flagged}),
            );
            super::super::apply_event(
                &mut task,
                &json!({"type":"error","message":"结账重新认证后仍要求登录"}),
            );
            assert_eq!(progress(&task), json!({"current":"prepare","completed":[]}));
            assert_eq!(task.status, "failed");
        }
        let mut flagged = fixture_task();
        stage(&mut flagged, "prepare");
        super::super::apply_event(
            &mut flagged,
            &json!({"type":"stage","message":"checkout","authentication_failed":true}),
        );
        assert_eq!(
            progress(&flagged),
            json!({"current":"prepare","completed":[]})
        );
        let mut task = fixture_task();
        stage(&mut task, "prepare");
        super::super::apply_event(
            &mut task,
            &json!({"type":"stage","message":"读取结算页失败"}),
        );
        assert_eq!(progress(&task), json!({"current":"prepare","completed":[]}));
    }

    #[test]
    fn retreat_clears_later_completion_without_inventing_unobserved_steps() {
        let mut task = fixture_task();
        for message in ["prepare", "checkout", "submit", "verify"] {
            stage(&mut task, message);
        }
        assert_eq!(
            progress(&task),
            json!({"current":"verify","completed":["prepare"]})
        );
        stage(&mut task, "submit");
        assert_eq!(
            progress(&task),
            json!({"current":"submit","completed":["prepare","verify"]})
        );
        stage(&mut task, "下单第 2/2 次尝试；已使用 1/1 次失败重试");
        assert_eq!(progress(&task), json!({"current":"prepare","completed":[]}));
    }

    #[test]
    fn pending_order_proof_completes_submit_but_mixed_unknown_never_completes_payment() {
        let mut task = fixture_task();
        task.action = "checkout".into();
        super::super::apply_event(
            &mut task,
            &json!({"type":"progress_result","result":{"status":"pending_payment","order_number":"W123"}}),
        );
        assert_eq!(
            progress(&task),
            json!({"current":"payment","completed":["submit"]})
        );
        stage(&mut task, "准备付款二维码");
        assert_eq!(
            progress(&task),
            json!({"current":"payment","completed":["submit"]})
        );
        super::super::apply_event(
            &mut task,
            &json!({"type":"progress_result","result":{"status":"queue_running",
            "pending_orders":[{"order_number":"W123"}],"jobs":[{"status":"submission_unknown"}]}}),
        );
        assert_eq!(progress(&task), json!({"current":"submit","completed":[]}));
        let mut missing = fixture_task();
        super::super::apply_event(
            &mut missing,
            &json!({"type":"result","result":{"success":true,"status":"pending_payment"}}),
        );
        assert!(missing.purchase_progress.is_none());
    }

    #[test]
    fn wrong_owner_unrelated_action_and_unrecognized_text_do_not_create_history() {
        for field in ["account_id", "task_id"] {
            let mut task = fixture_task();
            let mut event = json!({"type":"stage","message":"submit"});
            event[field] = json!("wrong");
            super::super::apply_event(&mut task, &event);
            assert!(task.purchase_progress.is_none());
            event["type"] = json!("progress_result");
            event["result"] = json!({"status":"pending_payment","order_number":"W123"});
            super::super::apply_event(&mut task, &event);
            assert!(task.purchase_progress.is_none());
        }
        let mut wrong_job = fixture_task();
        wrong_job.queue_job_ids = vec!["owned-job".into()];
        super::super::apply_event(
            &mut wrong_job,
            &json!({"type":"stage","message":"submit","queue_job_id":"wrong-job"}),
        );
        assert!(wrong_job.purchase_progress.is_none());
        let mut other = fixture_task();
        other.action = "login".into();
        stage(&mut other, "prepare");
        assert!(other.purchase_progress.is_none());
        let mut unrecognized = fixture_task();
        stage(&mut unrecognized, &format!("{}submit", "x".repeat(3000)));
        assert!(unrecognized.purchase_progress.is_none());
        super::super::apply_event(
            &mut unrecognized,
            &json!({"type":"result","result":{"success":true}}),
        );
        assert!(unrecognized.purchase_progress.is_none());
    }
}
