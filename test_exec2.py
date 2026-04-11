import os
import sys

if len(sys.argv) > 1 and sys.argv[1] == "reexec":
    print("Re-execed successfully!")
    sys.exit(0)

print(f"sys.executable: {sys.executable}")
print(f"sys.argv: {sys.argv}")
os.execv(sys.executable, [sys.executable] + sys.argv + ["reexec"])
