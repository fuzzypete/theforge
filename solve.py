import os
import re
import sys
import subprocess

print("================ START SOLVE.PY ================")

def run_cmd(cmd):
    print(f"\nRunning command: {cmd}")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(f"Exit code: {res.returncode}")
    print(f"STDOUT:\n{res.stdout}")
    print(f"STDERR:\n{res.stderr}")
    return res

# 1. Read files
print("Reading files...")
with open("src/theforge/config/auth.py", "r") as f:
    auth_src = f.read()

with open("src/theforge/cli/check_config.py", "r") as f:
    cc_src = f.read()

with open("src/theforge/config/model_identity.py", "r") as f:
    mi_src = f.read()

# 2. Print relevant original code
m_auth = re.search(r"def check_agent_auth", auth_src)
if m_auth:
    idx = auth_src[:m_auth.start()].count("\n")
    print(f"\ncheck_agent_auth start line: {idx}")
    lines = auth_src.splitlines()
    print("\n--- ORIGINAL check_agent_auth ---")
    print("\n".join(lines[idx:idx+100]))

m_sandbox = re.search(r"def _launcher_sandbox_readiness", auth_src)
if m_sandbox:
    idx = auth_src[:m_sandbox.start()].count("\n")
    print(f"\n_launcher_sandbox_readiness start line: {idx}")
    lines = auth_src.splitlines()
    print("\n--- ORIGINAL _launcher_sandbox_readiness ---")
    print("\n".join(lines[idx:idx+20]))

m_unconfirmed = re.search(r"def _unconfirmed_identity_models", cc_src)
if m_unconfirmed:
    idx = cc_src[:m_unconfirmed.start()].count("\n")
    print(f"\n_unconfirmed_identity_models start line: {idx}")
    lines = cc_src.splitlines()
    print("\n--- ORIGINAL _unconfirmed_identity_models ---")
    print("\n".join(lines[idx:idx+35]))

m_format = re.search(r"def _format_config", cc_src)
if m_format:
    idx = cc_src[:m_format.start()].count("\n")
    print(f"\n_format_config start line: {idx}")
    lines = cc_src.splitlines()
    print("\n--- ORIGINAL _format_config (first 100 lines) ---")
    print("\n".join(lines[idx:idx+100]))

print("\n--- All lines in check_config.py containing '✓ ready' ---")
for i, line in enumerate(cc_src.splitlines(), 1):
    if "✓ ready" in line:
        print(f"Line {i}: {line}")

# 3. Apply changes to auth.py
new_auth_src = auth_src

# Replace _launcher_sandbox_readiness return type annotation and return value
pattern_sandbox = r"def _launcher_sandbox_readiness\(profile: ModelProfile\) -> tuple\[bool, str\]:\s*\"\"\"[^\"]*\"\"\"\s*return \(True, \"\"\)"
replacement_sandbox = 'def _launcher_sandbox_readiness(profile: ModelProfile) -> tuple[bool | None, str]:\n    """CLI launchers are always auth-ready; binary presence checked separately."""\n    return (None, "unverified")'
new_auth_src = re.sub(pattern_sandbox, replacement_sandbox, new_auth_src)

# Replace check_agent_auth return type annotation
pattern_check_agent_auth = r"def check_agent_auth\([^)]*?\) -> tuple\[bool, str\]:"
def replace_signature(m):
    return m.group(0).replace("tuple[bool, str]", "tuple[bool | None, str]")
new_auth_src = re.sub(pattern_check_agent_auth, replace_signature, new_auth_src)

# Replace successful returns for CLI profiles within the CLI section of check_agent_auth
cli_section_match = re.search(r"if profile\.cli is not None:.*?(?=# ── API profiles)", new_auth_src, re.DOTALL)
if cli_section_match:
    cli_section = cli_section_match.group(0)
    # inside cli_section, replace return (True, "") with return (None, "unverified")
    new_cli_section = cli_section.replace('return (True, "")', 'return (None, "unverified")')
    new_auth_src = new_auth_src.replace(cli_section, new_cli_section)
else:
    print("WARNING: CLI section not found in auth.py!")

with open("src/theforge/config/auth.py", "w") as f:
    f.write(new_auth_src)
print("\nSuccessfully updated auth.py.")

# 4. Apply changes to check_config.py
new_cc_src = cc_src

# Replace the return type of _run_auth
new_cc_src = re.sub(
    r"def _run_auth\(([^)]*?)\) -> dict\[str, tuple\[bool, str\]\]:",
    r"def _run_auth(\1) -> dict[str, tuple[bool | None, str]]:",
    new_cc_src
)

# Insert _format_auth_status helper function before _format_config
format_config_match = re.search(r"def _format_config\(", new_cc_src)
if format_config_match:
    format_status_def = """def _format_auth_status(profile, ready, reason, config) -> str:
    if ready is None:
        auth_str = "unverified"
        unconfirmed = _unconfirmed_identity_models(config)
        model_val = getattr(profile, "model", None)
        model_key_val = getattr(profile, "model_key", None)
        is_unconfirmed = False
        if model_val and (model_val in unconfirmed or any(model_val in u for u in unconfirmed) or any(u in model_val for u in unconfirmed)):
            is_unconfirmed = True
        if model_key_val and (model_key_val in unconfirmed or any(model_key_val in u for u in unconfirmed) or any(u in model_key_val for u in unconfirmed)):
            is_unconfirmed = True
        if is_unconfirmed:
            auth_str += " (never checked against the provider's published model list)"
        return auth_str
    return "✓ ready" if ready else f"✗ {reason}"


"""
    new_cc_src = new_cc_src[:format_config_match.start()] + format_status_def + new_cc_src[format_config_match.start():]
else:
    print("WARNING: def _format_config not found!")

# Replace all occurrences of: auth_str = "✓ ready" if ready else f"✗ {reason}"
pattern_auth_str = r"auth_str\s*=\s*(['\"])✓ ready\1\s+if\s+ready\s+else\s+f\1✗ \{reason\}\1"
new_cc_src, count = re.subn(pattern_auth_str, "auth_str = _format_auth_status(profile, ready, reason, config)", new_cc_src)
print(f"\nReplaced {count} occurrences of auth_str rendering in check_config.py")

with open("src/theforge/cli/check_config.py", "w") as f:
    f.write(new_cc_src)
print("Successfully updated check_config.py.")

# 5. Check and update test files if they fail/need adjustment due to type or value changes
print("\nSearching for check_agent_auth in tests...")
test_files_with_auth = []
for root, dirs, files in os.walk("tests"):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            with open(path, "r", errors="ignore") as f:
                content = f.read()
                if "check_agent_auth" in content:
                    test_files_with_auth.append(path)

print(f"Test files calling check_agent_auth: {test_files_with_auth}")
for path in test_files_with_auth:
    with open(path, "r") as f:
        t_src = f.read()
    # Replace (True, "") assertions for CLI profiles
    # e.g., if we test a CLI profile in auth, we expect (None, "unverified") instead of (True, "")
    # Let's inspect what is asserted. If there is an assertion with a CLI profile, let's update it.
    # To be extremely safe, let's look at the actual code of these tests first.
    print(f"\n--- test file {path} contents with check_agent_auth ---")
    lines = t_src.splitlines()
    for idx, line in enumerate(lines):
        if "check_agent_auth" in line:
            start = max(0, idx - 5)
            end = min(len(lines), idx + 10)
            for j in range(start, end):
                print(f"  {j+1:4d}: {lines[j]}")

    # Let's update check_agent_auth assertions on CLI profiles in the test files
    # If the profile tested has profile.cli is not None, or if it checks CLI auth readiness
    # Let's see: typically tests use things like ModelProfile(..., cli="...") or model_profile(cli="...")
    # If we find `(True, "")` in lines where cli profiles are tested, we should replace it.
    # We can write a replace pattern if we see what's in the tests.
    # Since we can just run a regex replace on the file:
    # Let's see: we can replace assertions of check_agent_auth(cli_profile) == (True, "")
    # with (None, "unverified").
    # Let's do a replace of `(True, "")` with `(None, "unverified")` in test assertions that check CLI or npx
    # Let's look for test cases in test_auth.py. We will see them below in the run output.

# Let's run a dry run of pytest on auth/config first to see if there are any failures, before we update test files.
res_diff = run_cmd("git diff")

res_test = run_cmd("make dev-check")

# Let's check test failures and update test files if needed
if "FAIL" in res_test.stdout or "FAIL" in res_test.stderr or res_test.returncode != 0:
    print("\nTests failed! Let's analyze failures and patch test files.")
    # If the test failure is in test_auth.py:
    # Let's see if we can find test assertions of (True, "") for CLI and replace them with (None, "unverified")
    # For example, in tests/test_auth.py (or similar):
    # Let's open and inspect test_auth.py if it exists.
    # We can automatically search and replace:
    # We can write a python loop to fix the test assertions:
    for path in test_files_with_auth:
        with open(path, "r") as f:
            t_src = f.read()
        # If we see `assert check_agent_auth(profile) == (True, "")` or similar:
        # Let's find any check_agent_auth calls that expect (True, "") where profile is a CLI profile
        # Since we want to make it robust, we can inspect and replace:
        # Let's replace any `(True, "")` with `(None, "unverified")` for CLI-related test lines
        # Let's see the exact failure message first. We can do this in python by running pytest and reading stderr/stdout.

# Let's write the updated files and run git diff to verify
print("\n=== FINAL GIT DIFF ===")
run_cmd("git diff")

# Let's run make dev-check again to see if everything is clean
res_test2 = run_cmd("make dev-check")
if res_test2.returncode == 0:
    print("\nTests passed successfully!")
    # Commit
    run_cmd("git add src/")
    for path in test_files_with_auth:
        # if we modified any tests
        run_cmd(f"git add {path}")
    run_cmd('git commit -m "feat(cli): report unverified status for CLI-transport profiles and show model unconfirmed identity caveat"')
else:
    print("\nTests failed on the second run!")

print("================ END SOLVE.PY ================")
