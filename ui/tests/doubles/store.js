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
// Stands in for CloudStack's Vuex store so the section config can be
// tested outside their build. The shipped file imports '@/store'
// exactly as every other CloudStack section does, so it drops into
// their ui/ tree unchanged; only the test alias points here.
const state = {
  userInfo: { roletype: 'Admin', account: 'admin', domainid: '1' },
  features: {},
  apis: {},
  listAllProjects: false
}

export function asRole (roletype) {
  state.userInfo.roletype = roletype
}

export function allowApis (names) {
  state.apis = Object.fromEntries(names.map((name) => [name, {}]))
}

export default {
  getters: {
    get userInfo () { return state.userInfo },
    get features () { return state.features },
    get apis () { return state.apis },
    get listAllProjects () { return state.listAllProjects }
  }
}
