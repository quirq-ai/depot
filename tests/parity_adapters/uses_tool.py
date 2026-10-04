"""A kind whose test runs a program from its pinned toolchain, so it passes only after qq sync."""
from qqrecipes.contract import Adapter


class UsesTool(Adapter):
    kind = "uses-tool"
    toolchain = "hello"

    def test(self, target, ctx):
        return [self.action(target, ctx, "test", "hello", ["{toolchain:hello}hello"])]


ADAPTER = UsesTool()
