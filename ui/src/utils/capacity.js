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
// The GPU capacity heatmap (SPEC.md section 9).
//
// "Heatmap by zone and class: allocated, free, unhealthy, quarantined."
// The point of a heatmap is that an operator takes it in at a glance,
// so the status a cell gets is what decides its colour — and broken
// hardware has to win over a merely busy class, or a failing GPU hides
// behind a green cell.

/** Fraction of a class's GPUs that are allocated, 0 to 1. */
function utilisationOf (capacity) {
  // A declared class whose hosts are not tagged yet has no GPUs at all.
  // Dividing by zero here puts NaN% on the operator's first screen.
  if (!capacity.total) return 0
  return capacity.allocated / capacity.total
}

export function cellFor (capacity) {
  const utilisation = utilisationOf(capacity)

  let status = 'healthy'
  if (capacity.unhealthy > 0) {
    // Outranks everything: a GPU in this state is not coming back
    // without a drain, and a quiet class should not hide it.
    status = 'unhealthy'
  } else if (capacity.free === 0 && capacity.total > 0) {
    status = 'exhausted'
  } else if (utilisation >= 0.9) {
    status = 'tight'
  }

  return {
    zone: capacity.zone,
    gpuclass: capacity.gpuclass,
    total: capacity.total,
    allocated: capacity.allocated,
    free: capacity.free,
    unhealthy: capacity.unhealthy,
    utilisation,
    status
  }
}

/** One row per zone, one cell per GPU class, in a stable order. */
export function heatmapRows (capacities) {
  const zones = new Map()
  for (const capacity of capacities) {
    if (!zones.has(capacity.zone)) zones.set(capacity.zone, [])
    zones.get(capacity.zone).push(cellFor(capacity))
  }
  return [...zones.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([zone, cells]) => ({
      zone,
      cells: cells.sort((a, b) => a.gpuclass.localeCompare(b.gpuclass))
    }))
}
