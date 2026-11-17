// Licensed to the Apache Software Foundation (ASF) under one or more
// contributor license agreements. See the NOTICE file distributed with this
// work for additional information regarding copyright ownership. The ASF
// licenses this file to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance with the
// License. You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
// WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
// License for the specific language governing permissions and limitations
// under the License.

/**
 * A column header in a CloudStack list, by the text a user reads.
 *
 * `getByRole('columnheader', …)` does not work here, and the way it
 * fails is the problem: Ant's table puts the header text in a span
 * inside the `th`, and the accessibility tree exposes that span as
 * `generic`, so the locator matches nothing however long it waits.
 * Eight assertions across two specs read as "this column is missing"
 * for columns plainly on the screen, and raising the timeout only
 * made them fail more slowly.
 *
 * Scoped to `thead` so a value in a row cannot satisfy a claim about
 * a column.
 */
export function header (page, text) {
  return page.locator('thead').getByText(text)
}

/**
 * A row's link into its detail view, by the text in the first column.
 *
 * Clicking the row does nothing: CloudStack puts the link on the
 * first column's content, and only when that column is keyed `name`.
 */
export function rowLink (page, text) {
  return page.locator('tbody').getByRole('link', { name: text })
}

/**
 * An action button, by the icon it carries.
 *
 * CloudStack's action buttons are icon-only — `<button><span
 * role="img" aria-label="reload">` — and the words a person reads
 * ("Replace certificate") are in a tooltip rendered on hover. So
 * `getByRole('button', { name: /replace certificate/i })` matches
 * nothing: the accessible name is "reload". The icon is what the DOM
 * actually offers, and it is the same string the section config
 * names in `icon:`.
 */
export function action (page, icon) {
  return page.locator(`.ant-btn:has(.anticon-${icon})`)
}
