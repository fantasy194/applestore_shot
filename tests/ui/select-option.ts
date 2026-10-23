import { expect, type Locator } from "@playwright/test";

/** Choose by the same visible label used by touch and keyboard users. */
export async function chooseOption(field: Locator, label: string) {
  await field.click();
  await field.page().getByRole("option", { name: label, exact: true }).click();
  await expect(field).toHaveText(label);
}
