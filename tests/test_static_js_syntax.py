"""A syntax check for every `static/js/*.js` file, run with no Node.

The project has no JavaScript build step and no Node runtime, but macOS
ships a JavaScript engine of its own: `osascript -l JavaScript` (JXA) can
read a file's text and hand it to `new Function(source)`, which throws
`SyntaxError` for the same broken code a browser would refuse to parse.
This is a syntax check only - it never executes any of the project's own
scripts, since none of them do anything until a `[data-*]` root they
guard against exists in the DOM.

Skipped where `osascript` doesn't exist (Linux CI has no macOS engine to
call), so this test proves nothing there - the syntax is instead proven by
every one of these scripts already loading correctly in the same CI job's
browser-free page tests.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_JS_DIR = Path(__file__).resolve().parents[1] / "src" / "marrquee" / "static" / "js"

# Reads the target file's text through Foundation's NSString (so encoding
# is handled the same way a browser would) and hands it to `new Function`,
# which parses without ever running the script - a `SyntaxError` there is
# the same error a browser's own parser would raise.
_CHECKER_SOURCE = """
function run(argv) {
  var path = argv[0];
  ObjC.import("Foundation");
  var nsstring = $.NSString.stringWithContentsOfFileEncodingError(
    path, $.NSUTF8StringEncoding, null
  );
  var source = ObjC.unwrap(nsstring);
  try {
    new Function(source);
    return "ok";
  } catch (error) {
    return String(error);
  }
}
"""


def test_every_static_script_parses(tmp_path: Path) -> None:
    if shutil.which("osascript") is None:
        pytest.skip("osascript (macOS JavaScriptCore) is not available on this machine")

    checker_path = tmp_path / "syntax_checker.js"
    checker_path.write_text(_CHECKER_SOURCE)

    scripts = sorted(_JS_DIR.glob("*.js"))
    assert scripts, "expected at least one static/js/*.js file to check"

    for script_path in scripts:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", str(checker_path), str(script_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "ok", f"{script_path.name}: {result.stdout.strip()}"
