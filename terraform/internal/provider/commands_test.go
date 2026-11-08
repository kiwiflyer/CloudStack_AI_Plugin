package provider

import (
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"testing"
)

// The provider calls CloudStack commands by name, as string literals.
// A renamed command therefore leaves a resource that builds, tests,
// plans — and fails at apply with CloudStack reporting an unknown
// command, which reads like a provider bug.
//
// CLAUDE.md: commands are generated from aisvc/api/commands.py and
// never hand-listed. That has to hold across this boundary too, which
// means reading the map rather than trusting the literals. The UI makes
// the same check from its own side.

// A whole command name: the closing quote is followed by the comma
// that ends the argument. `Call("createAi"+title+"Profile", ...)` is
// deliberately not matched here — it is composed, and COMPOSED below
// says what it can produce.
var commandCall = regexp.MustCompile(`Call(?:Async)?\(\s*"([A-Za-z]+)"\s*,`)
var engineCommand = regexp.MustCompile(`_c\(\s*"([A-Za-z]+)"`)

// CloudStack's own, not ours; they are not in the AI command map.
var CORE_COMMANDS = map[string]bool{"queryAsyncJobResult": true}

// What profileResource builds by concatenation. Written out because a
// scan cannot resolve it, and leaving these unchecked would mean the
// guard covered everything except the one place names are assembled.
var COMPOSED = []string{
	"createAiGenerationProfile", "listAiGenerationProfiles",
	"updateAiGenerationProfile", "deleteAiGenerationProfile",
	"createAiServingProfile", "listAiServingProfiles",
	"updateAiServingProfile", "deleteAiServingProfile",
}

// commandsCalled finds every command name this package calls.
func commandsCalled(t *testing.T) []string {
	t.Helper()

	names := map[string]bool{}
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("reading the package: %v", err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if filepath.Ext(name) != ".go" || len(name) > 8 && name[len(name)-8:] == "_test.go" {
			continue
		}
		source, err := os.ReadFile(name)
		if err != nil {
			t.Fatalf("reading %s: %v", name, err)
		}
		for _, match := range commandCall.FindAllSubmatch(source, -1) {
			name := string(match[1])
			if !CORE_COMMANDS[name] {
				names[name] = true
			}
		}
	}

	for _, name := range COMPOSED {
		names[name] = true
	}

	out := make([]string, 0, len(names))
	for name := range names {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// engineCommands reads the command map itself.
func engineCommands(t *testing.T) map[string]bool {
	t.Helper()

	// Tests run in the package directory; the map is three up.
	source, err := os.ReadFile(filepath.Join("..", "..", "..", "aisvc", "api", "commands.py"))
	if err != nil {
		t.Fatalf("reading the engine command map: %v", err)
	}

	known := map[string]bool{}
	for _, match := range engineCommand.FindAllSubmatch(source, -1) {
		known[string(match[1])] = true
	}
	return known
}

func TestTheCommandMapWasFound(t *testing.T) {
	// Otherwise an empty map would make the check below vacuous.
	if got := len(engineCommands(t)); got < 20 {
		t.Fatalf("only %d commands found; the map was not read properly", got)
	}
}

func TestTheProviderCallsSomething(t *testing.T) {
	if got := len(commandsCalled(t)); got < 15 {
		t.Fatalf("only %d command calls found; the scan is not working", got)
	}
}

func TestEveryCommandTheProviderCallsExists(t *testing.T) {
	known := engineCommands(t)

	for _, name := range commandsCalled(t) {
		if !known[name] {
			t.Errorf("%s is called but is not in aisvc/api/commands.py", name)
		}
	}
}

func TestTheComposedProfileCommandsAreRealToo(t *testing.T) {
	// The names profileResource assembles never appear whole in the
	// source, so this is the only thing checking they exist.
	known := engineCommands(t)

	for _, name := range COMPOSED {
		if !known[name] {
			t.Errorf("profileResource would build %s, which is not a command", name)
		}
	}
}

func TestItWouldNoticeARenamedCommand(t *testing.T) {
	// The guard is only worth having if it fails, so: a name that is
	// not in the map must not be reported as known.
	known := engineCommands(t)

	if known["listAiAdaptersRenamed"] {
		t.Fatal("the check would accept anything")
	}
	if !known["listAiAdapters"] {
		t.Fatal("a command that does exist was not found; the regexp is wrong")
	}
}

func TestTheV2CommandsAreAllReachable(t *testing.T) {
	// Every v2 command the UI offers should be expressible here too —
	// CLAUDE.md requires that everything in the UI also works through
	// CloudMonkey and Terraform.
	called := map[string]bool{}
	for _, name := range commandsCalled(t) {
		called[name] = true
	}

	for _, name := range []string{
		"listAiAdapters", "uploadAiAdapter", "deleteAiAdapter",
		"createAiBatch", "listAiBatches", "cancelAiBatch",
		"listAiCertificates", "uploadAiCertificate", "replaceAiCertificate",
		"deleteAiCertificate",
		"listAiPrivateEndpoints", "createAiPrivateEndpoint", "deleteAiPrivateEndpoint",
		"listAiGuardrailProfiles", "createAiGuardrailProfile",
		"updateAiGuardrailProfile", "deleteAiGuardrailProfile",
		"listAiEvaluations",
	} {
		if !called[name] {
			t.Errorf("no resource or data source calls %s", name)
		}
	}
}
