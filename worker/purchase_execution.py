"""One purchase-attempt dispatcher shared by manual, scheduled and queue work.

Authentication, retry budgets and persistence belong to the caller. Isolation
changes the worker transport only; every leaf executes the same purchase_sku
and the same Service configuration/checkout authentication contract.
"""
import duo_killer


def execute_purchase_attempt(service, params, *, isolated=False, parallel_executor=None, serial_executor=None):
    from parallel_checkout import concurrency, run_parallel_purchase
    from service import quantity
    count = quantity(params)
    requested_routes = concurrency({'concurrency':params.get('requested_concurrency',params.get('concurrency',1))})
    concurrency(params)
    # One account owns one mutable Apple bag/Checkout. Route count never
    # becomes extra purchases or an implicit retry budget.
    routes = 1
    params = {**params,'concurrency':1}
    preparing = params.get('prepare_only') is True
    retail = params.get('fulfillment', 'HOME') == 'RETAIL'
    if preparing:
        if isolated:
            raise ValueError('提前准备不能创建隔离下单子任务。')
        if getattr(service, 'automatic_checkout', False) is not True:
            raise ValueError('提前准备需要已启用自动结算配置。')
        return duo_killer.purchase_sku(params.get('sku', 'MK2M4CH'), quantity=count, prepare_only=True)
    if isolated or retail and routes > 1:
        if not retail or params.get('_parallel_child') is True:
            raise ValueError('隔离并发下单仅适用于父任务的正式自提购买。')
        if parallel_executor is not None:
            result = parallel_executor(service, params)
        else:
            result = run_parallel_purchase(service, params, preflight_complete=True)
    elif serial_executor is not None:
        result = serial_executor(service, params)
    else:
        result = duo_killer.purchase_sku(params.get('sku', 'MK2M4CH'), quantity=count)
    if isinstance(result,dict):
        result['requested_concurrency']=requested_routes
        result['effective_concurrency']=1
    return result
