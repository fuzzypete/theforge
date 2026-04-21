from dataclasses import dataclass, replace

@dataclass
class Foo:
    a: int
    b: int = 0

    def __post_init__(self):
        self.b = self.a * 2

f = Foo(1)
print(f.b)
f2 = replace(f, a=2)
print(f2.b)
