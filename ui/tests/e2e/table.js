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
