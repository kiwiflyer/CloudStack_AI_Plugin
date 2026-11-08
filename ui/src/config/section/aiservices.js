// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.

// The AI Services section (SPEC.md section 9).
//
// Declarative on purpose. CloudStack builds list views, detail views,
// search, pagination, async job notifications and role-based action
// visibility from this config, so a screen described here inherits all
// of it and behaves like every other section — which is the whole
// reason for not hand-rolling the screens.
//
// Tenant screens come first: milestone 6 is "tenant first", and a
// tenant landing on an admin list they cannot read is a bad first
// impression of the product.
//
// Every `api` below must exist in aisvc/api/commands.py; a unit test
// holds the two together, because a renamed command would otherwise
// leave a button that fails with no sign of why.

import { defineAsyncComponent, shallowRef } from 'vue'
import store from '@/store'

const role = () => store.getters.userInfo.roletype
const isAdmin = () => role() === 'Admin'
const isAdminOrDomainAdmin = () => ['Admin', 'DomainAdmin'].includes(role())

const view = (name) =>
  shallowRef(defineAsyncComponent(() => import(`@/views/aiservices/${name}.vue`)))

const detailsTab = {
  name: 'details',
  component: shallowRef(
    defineAsyncComponent(() => import('@/components/view/DetailsTab.vue'))
  )
}

export default {
  name: 'aiservices',
  title: 'label.aiservices',
  icon: 'experiment-outlined',
  // Visible to everyone with a role: a tenant sees their catalog and
  // endpoints, an admin sees those plus the operator screens.
  show: () => ['Admin', 'DomainAdmin', 'User'].includes(role()),
  children: [
    // -- tenant ---------------------------------------------------------
    {
      name: 'aimodelcatalog',
      title: 'label.ai.model.catalog',
      icon: 'appstore-outlined',
      permission: ['listAiModels'],
      resourceType: 'AiModel',
      columns: () => ['name', 'task', 'size', 'license', 'pricetier'],
      details: () => [
        'id', 'name', 'task', 'size', 'license', 'pricetier', 'contextlength',
        'description'
      ],
      tabs: [detailsTab],
      actions: [
        {
          api: 'deployAiModel',
          icon: 'rocket-outlined',
          label: 'label.ai.deploy.model',
          dataView: true,
          popup: true,
          args: ['zoneid', 'name', 'replicas', 'servingprofile']
        }
      ]
    },
    {
      name: 'aiendpoint',
      title: 'label.ai.endpoints',
      icon: 'api-outlined',
      permission: ['listAiEndpoints'],
      resourceType: 'AiEndpoint',
      columns: () => {
        const fields = ['name', 'model', 'state', 'replicas', 'baseurl']
        // Only an operator benefits from the owner column; for a tenant
        // every row is theirs and it is a wasted column.
        if (isAdminOrDomainAdmin()) fields.push('account')
        return fields
      },
      details: () => [
        'id', 'name', 'model', 'state', 'replicas', 'baseurl', 'pricetier',
        'account', 'domainpath', 'created'
      ],
      tabs: [
        detailsTab,
        {
          name: 'label.ai.api.keys',
          component: view('ApiKeysTab')
        },
        {
          name: 'label.ai.snippets',
          component: view('SnippetsTab')
        }
      ],
      actions: [
        {
          api: 'updateAiModelDeployment',
          icon: 'edit-outlined',
          label: 'label.ai.update.deployment',
          dataView: true,
          popup: true,
          args: ['replicas', 'servingprofile']
        },
        {
          api: 'createAiApiKey',
          icon: 'key-outlined',
          label: 'label.ai.create.api.key',
          dataView: true,
          popup: true,
          args: ['name', 'allowedmodels', 'cidrs', 'expiresat', 'ratelimitrpm']
        },
        {
          api: 'deleteAiModelDeployment',
          icon: 'delete-outlined',
          label: 'label.ai.delete.deployment',
          dataView: true,
          // CloudStack asks for confirmation; this is not reversible.
          message: 'message.ai.delete.deployment'
        }
      ]
    },
    {
      name: 'aiplayground',
      title: 'label.ai.playground',
      icon: 'message-outlined',
      permission: ['createAiPlaygroundSession'],
      component: view('Playground'),
      columns: () => ['model'],
      details: () => ['model'],
      actions: []
    },
    {
      name: 'aigenerationprofile',
      title: 'label.ai.generation.profiles',
      icon: 'sliders-outlined',
      permission: ['listAiGenerationProfiles'],
      resourceType: 'AiGenerationProfile',
      columns: () => {
        const fields = ['name', 'scope', 'policy']
        if (isAdminOrDomainAdmin()) fields.push('account')
        return fields
      },
      details: () => ['id', 'name', 'description', 'scope', 'policy', 'account'],
      tabs: [detailsTab],
      actions: [
        {
          api: 'createAiGenerationProfile',
          icon: 'plus-outlined',
          label: 'label.ai.create.generation.profile',
          listView: true,
          popup: true,
          args: ['name', 'description', 'temperature', 'topp', 'maxtokens', 'policy']
        },
        {
          api: 'updateAiGenerationProfile',
          icon: 'edit-outlined',
          label: 'label.ai.update.generation.profile',
          dataView: true,
          popup: true,
          args: ['temperature', 'topp', 'maxtokens', 'policy']
        },
        {
          api: 'assignAiTuningProfile',
          icon: 'link-outlined',
          label: 'label.ai.assign.profile',
          dataView: true,
          popup: true,
          args: ['scope', 'domainid', 'account', 'deploymentid']
        },
        {
          api: 'deleteAiGenerationProfile',
          icon: 'delete-outlined',
          label: 'label.ai.delete.generation.profile',
          dataView: true,
          message: 'message.ai.delete.generation.profile'
        }
      ]
    },
    {
      name: 'aieffectiveparameters',
      title: 'label.ai.effective.parameters',
      icon: 'function-outlined',
      permission: ['getAiEffectiveParameters'],
      // A resolved value is meaningless without the level it came from
      // (SPEC.md section 6), and a plain detail view cannot show that
      // pairing, so this screen is its own component.
      component: view('EffectiveParameters'),
      columns: () => ['parameter', 'value', 'source'],
      details: () => ['parameter', 'value', 'source', 'policy'],
      actions: []
    },
    {
      name: 'aiusage',
      title: 'label.ai.usage.budgets',
      icon: 'dollar-outlined',
      permission: ['listAiUsage'],
      resourceType: 'AiUsage',
      columns: () => {
        const fields = ['model', 'inputtokens', 'outputtokens', 'gpuhours', 'cost']
        if (isAdminOrDomainAdmin()) fields.push('account')
        return fields
      },
      details: () => [
        'account', 'model', 'inputtokens', 'outputtokens', 'gpuhours', 'cost',
        'currency', 'recorded'
      ],
      tabs: [
        detailsTab,
        {
          name: 'label.ai.budgets',
          component: view('BudgetsTab')
        }
      ],
      actions: [
        {
          api: 'createAiBudget',
          icon: 'plus-outlined',
          label: 'label.ai.create.budget',
          listView: true,
          popup: true,
          // Admin only: a tenant able to set its own budget could lift
          // the limit that is blocking it.
          show: isAdminOrDomainAdmin,
          args: ['account', 'monthly', 'currency', 'onexceed']
        },
        {
          api: 'updateAiBudget',
          icon: 'edit-outlined',
          label: 'label.ai.update.budget',
          dataView: true,
          popup: true,
          show: isAdminOrDomainAdmin,
          args: ['monthly', 'currency', 'onexceed']
        }
      ]
    },

    // -- admin ----------------------------------------------------------
    {
      name: 'aisetup',
      title: 'label.ai.setup',
      icon: 'tool-outlined',
      permission: ['listAiSetupChecks'],
      show: isAdmin,
      // A wizard is a sequence, not a list of rows.
      component: view('SetupWizard'),
      columns: () => ['name', 'status', 'detail'],
      details: () => ['name', 'status', 'detail', 'remedy'],
      actions: [
        {
          api: 'runAiSetupSelfTest',
          icon: 'experiment-outlined',
          label: 'label.ai.run.self.test',
          listView: true,
          popup: true,
          args: ['zone']
        },
        {
          api: 'discoverAiGpuClasses',
          icon: 'search-outlined',
          label: 'label.ai.discover.gpu.classes',
          listView: true
        }
      ]
    },
    {
      name: 'aihuggingface',
      title: 'label.ai.huggingface',
      icon: 'cloud-download-outlined',
      permission: ['searchAiHfModels'],
      show: isAdminOrDomainAdmin,
      component: view('HuggingFaceBrowser'),
      columns: () => ['repo', 'task', 'license', 'downloads', 'compatible'],
      details: () => ['repo', 'revision', 'task', 'license', 'params', 'problems'],
      actions: [
        {
          api: 'checkAiHfModel',
          icon: 'safety-certificate-outlined',
          label: 'label.ai.check.compatibility',
          dataView: true,
          popup: true,
          args: ['repo', 'revision']
        },
        {
          api: 'mirrorAiModel',
          icon: 'cloud-download-outlined',
          label: 'label.ai.mirror.model',
          dataView: true,
          popup: true,
          args: ['repo', 'revision']
        },
        {
          api: 'approveAiModel',
          icon: 'check-outlined',
          label: 'label.ai.approve.model',
          dataView: true,
          show: isAdmin,
          message: 'message.ai.approve.model'
        }
      ]
    },
    {
      name: 'airegistry',
      title: 'label.ai.registry',
      icon: 'database-outlined',
      permission: ['listAiModels'],
      show: isAdminOrDomainAdmin,
      resourceType: 'AiModel',
      columns: () => ['name', 'repo', 'revision', 'license', 'sizegb', 'deployments'],
      details: () => [
        'id', 'name', 'repo', 'revision', 'sha256', 'license', 'sizegb',
        'task', 'published', 'deployments'
      ],
      tabs: [detailsTab],
      actions: [
        {
          api: 'publishAiModel',
          icon: 'global-outlined',
          label: 'label.ai.publish.model',
          dataView: true,
          popup: true,
          args: ['pricetier', 'domainid']
        }
      ]
    },
    {
      name: 'aicapacity',
      title: 'label.ai.gpu.capacity',
      icon: 'hdd-outlined',
      permission: ['listAiGpuCapacity'],
      show: isAdmin,
      // SPEC.md section 9 asks for a heatmap by zone and class, which
      // is not a table.
      component: view('GpuCapacity'),
      columns: () => ['zone', 'gpuclass', 'total', 'allocated', 'free', 'unhealthy'],
      details: () => [
        'zone', 'gpuclass', 'hosttag', 'total', 'allocated', 'free',
        'unhealthy', 'nodes'
      ],
      actions: []
    },
    {
      name: 'aifleet',
      title: 'label.ai.fleet.health',
      icon: 'cluster-outlined',
      permission: ['listAiGpuCapacity'],
      show: isAdmin,
      component: view('FleetHealth'),
      columns: () => ['host', 'template', 'driver', 'state', 'drift'],
      details: () => ['host', 'template', 'driver', 'state', 'drift', 'nodes'],
      actions: [
        {
          api: 'drainAiNode',
          icon: 'logout-outlined',
          label: 'label.ai.drain.node',
          dataView: true,
          popup: true,
          // Destroy, not stop: a stopped node keeps its GPU allocated
          // (ADR-0003), so the confirmation has to say so.
          message: 'message.ai.drain.node',
          args: ['force']
        }
      ]
    },
    {
      name: 'airollout',
      title: 'label.ai.rollouts',
      icon: 'swap-outlined',
      permission: ['listAiModelDeployments'],
      show: isAdminOrDomainAdmin,
      resourceType: 'AiDeployment',
      columns: () => ['name', 'model', 'livecolour', 'candidate', 'weight', 'state'],
      details: () => [
        'id', 'name', 'model', 'livecolour', 'candidate', 'weight', 'state',
        'spechash'
      ],
      tabs: [detailsTab],
      actions: [
        {
          api: 'promoteAiRollout',
          icon: 'check-circle-outlined',
          label: 'label.ai.promote.rollout',
          dataView: true,
          message: 'message.ai.promote.rollout'
        },
        {
          api: 'rollbackAiRollout',
          icon: 'rollback-outlined',
          label: 'label.ai.rollback.rollout',
          dataView: true,
          message: 'message.ai.rollback.rollout'
        }
      ]
    },
    {
      name: 'aiservingprofile',
      title: 'label.ai.serving.profiles',
      icon: 'control-outlined',
      permission: ['listAiServingProfiles'],
      // Serving profiles size vLLM against the hardware; a tenant
      // changing one could take a GPU host down (SPEC.md section 7).
      show: isAdmin,
      resourceType: 'AiServingProfile',
      columns: () => ['name', 'maxnumseqs', 'gpumemoryutilization', 'maxmodellen'],
      details: () => [
        'id', 'name', 'description', 'maxnumseqs', 'gpumemoryutilization',
        'maxmodellen', 'tensorparallel'
      ],
      tabs: [detailsTab],
      actions: [
        {
          api: 'createAiServingProfile',
          icon: 'plus-outlined',
          label: 'label.ai.create.serving.profile',
          listView: true,
          popup: true,
          args: [
            'name', 'description', 'maxnumseqs', 'gpumemoryutilization',
            'maxmodellen', 'tensorparallel'
          ]
        },
        {
          api: 'updateAiServingProfile',
          icon: 'edit-outlined',
          label: 'label.ai.update.serving.profile',
          dataView: true,
          popup: true,
          args: ['maxnumseqs', 'gpumemoryutilization', 'maxmodellen']
        },
        {
          api: 'deleteAiServingProfile',
          icon: 'delete-outlined',
          label: 'label.ai.delete.serving.profile',
          dataView: true,
          message: 'message.ai.delete.serving.profile'
        }
      ]
    },
    {
      name: 'aiactivity',
      title: 'label.ai.activity',
      icon: 'history-outlined',
      permission: ['listAiReconcilerRuns'],
      show: isAdminOrDomainAdmin,
      resourceType: 'AiReconcilerRun',
      columns: () => ['id', 'zone', 'status', 'actions', 'started', 'finished'],
      details: () => [
        'id', 'zone', 'status', 'actions', 'started', 'finished', 'error'
      ],
      tabs: [
        detailsTab,
        {
          name: 'label.ai.run.actions',
          component: view('RunActionsTab')
        }
      ],
      actions: []
    }
  ]
}
