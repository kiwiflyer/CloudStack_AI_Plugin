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
