"""A kind whose build copies its sources and whose test writes JUnit with one case per source."""
from qqrecipes.contract import Adapter

TEST = r"""
import os, pathlib, sys
from xml.sax.saxutils import quoteattr
files = sys.argv[1:]
cases = []
for f in files:
    ok = pathlib.Path(f).read_text().strip() != "bad"
    body = "" if ok else '<failure message="bad content"/>'
    cases.append(f'<testcase classname="check" name={quoteattr(f)} time="0.001">{body}</testcase>')
failures = sum("failure" in c for c in cases)
out = pathlib.Path(os.environ["OUT"]) / "check.xml"
out.write_text(f'<testsuite name="check" tests="{len(cases)}" failures="{failures}">' + "".join(cases) + "</testsuite>")
sys.exit(1 if failures else 0)
"""


class CheckFiles(Adapter):
    kind = "check-files"

    def build(self, target, ctx):
        return [self.action(target, ctx, "build", "copy",
                            ["sh", "-c", "mkdir -p build-out && cat \"$@\" > build-out/all", "sh", *target.srcs],
                            outputs=("build-out",))]

    def test(self, target, ctx):
        return [self.action(target, ctx, "test", "check", ["python3", "-c", TEST, *target.srcs],
                            env={"OUT": "{out}/junit"}, junit="junit/check.xml")]


ADAPTER = CheckFiles()
