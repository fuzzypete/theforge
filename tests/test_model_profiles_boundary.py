"""The accumulation / read-model ownership boundary in model profiles (#2467).

``model_profiles`` used to hold two change-reasons behind one ownership:
accumulating a finished run's outcome into stored history, and deriving the
signals routing consults from that history. It is now a re-export facade over
two owners plus the vocabulary they share:

- ``model_profiles_storage``     — carriers, I/O, ``apply_run``, reset, migration
- ``model_profiles_read_model``  — ``get_*_signal`` and the stat readers
- ``model_profiles_identity``    — constants and identity resolution used by both

These tests pin the *ownership*, not the file sizes. The property that matters
is that adding a routing signal and changing how an outcome is folded are
independent changes: neither module has to be opened to make the other kind of
change. Behavioural equivalence of the moved code is covered by
``test_model_profiles.py`` (accumulation, through the facade) and
``test_model_profiles_read_model.py`` (signals, over directly-built state).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from theforge import model_profiles as facade
from theforge import model_profiles_identity as identity
from theforge import model_profiles_read_model as read_model
from theforge import model_profiles_storage as storage


def _imported_modules(module: object) -> set[str]:
    """Every module name imported anywhere in ``module``, including lazily."""
    tree = ast.parse(Path(inspect.getfile(module)).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


class TestReadModelDoesNotImportAccumulation:
    """AC1: the signals are readable from a module that never sees storage."""

    def test_read_model_has_no_import_of_the_storage_module(self) -> None:
        """Not at module scope, and not lazily inside a function either.

        A lazy import would satisfy an import-graph check while leaving the
        coupling exactly where it was — so the whole source is scanned, not
        just the header.
        """
        assert not [
            name for name in _imported_modules(read_model) if "model_profiles_storage" in name
        ]

    def test_importing_the_read_model_does_not_load_storage(self) -> None:
        """Fresh-interpreter check: nothing drags storage in transitively."""
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import theforge.model_profiles_read_model as rm; "
                "print('theforge.model_profiles_storage' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "False"

    def test_read_model_defines_no_accumulators(self) -> None:
        """``apply_run`` and the fold helpers are storage's, whole."""
        names = vars(read_model)
        assert "apply_run" not in names
        assert "RunOutcome" not in names
        assert not [n for n in names if n.startswith(("_fold_", "_update_", "_merge_", "_zero_"))]

    def test_read_model_performs_no_profile_io(self) -> None:
        """Signals take the profiles dict as an argument; they never load it."""
        assert "load_profiles" not in vars(read_model)
        assert "save_profiles" not in vars(read_model)
        assert "yaml" not in _imported_modules(read_model)


class TestAccumulationDoesNotImportTheReadModel:
    """The reverse direction, so neither half can quietly claim the other."""

    def test_storage_has_no_import_of_the_read_model(self) -> None:
        assert not [
            name for name in _imported_modules(storage) if "model_profiles_read_model" in name
        ]

    def test_storage_defines_no_routing_signals(self) -> None:
        assert not [n for n in vars(storage) if n.startswith("get_") and n.endswith("_signal")]

    def test_shared_vocabulary_depends_on_neither(self) -> None:
        """``model_profiles_identity`` is below both, not between them."""
        imported = _imported_modules(identity)
        assert not [n for n in imported if "model_profiles_storage" in n]
        assert not [n for n in imported if "model_profiles_read_model" in n]


class TestOneBindingPerName:
    """The facade re-exports one binding per name, not two divergent ones."""

    def test_constants_have_a_single_owner(self) -> None:
        """A constant read by both halves is defined once and imported twice.

        Two definitions would let the facade re-export one binding while an
        owner used the other — the divergence a mechanical move invites.
        """
        for name in (
            "ROLES",
            "COMPLEXITY_BANDS",
            "ALIAS_DERIVED_KEY",
            "CAPABILITY_RECENCY_WINDOW",
        ):
            assert name in vars(identity)
            owner = getattr(identity, name)
            for module in (storage, read_model, facade):
                if hasattr(module, name):
                    assert getattr(module, name) is owner

    def test_facade_reexports_the_owning_module_object(self) -> None:
        assert facade.apply_run is storage.apply_run
        assert facade.get_dev_signal is read_model.get_dev_signal
        assert facade._normalize_band is identity._normalize_band

    def test_every_exported_name_resolves(self) -> None:
        """``__all__`` is the compatibility contract; nothing in it may dangle."""
        missing = [name for name in facade.__all__ if not hasattr(facade, name)]
        assert missing == []


class TestSignalNeedsNoRunOutcome:
    """AC2: a signal is exercisable against directly-constructed stored state."""

    def test_dev_signal_reads_a_hand_built_profile(self) -> None:
        profiles = {
            "models": {
                "sonnet": {
                    "dev": {
                        "runs": 4,
                        "by_complexity": {
                            "medium": {
                                "runs": 4,
                                "_successes": 3,
                                "success_rate": 0.75,
                                "_recent": [1, 1, 0, 1],
                            }
                        },
                    }
                }
            }
        }
        signal = read_model.get_dev_signal(profiles, "sonnet", "medium", 2)
        assert signal["runs"] == 4
        assert signal["raw"] == 0.75
        # No RunOutcome was built and no run was applied to produce this state.
        assert "RunOutcome" not in vars(read_model)


class TestNewSignalIsAOneModuleChange:
    """AC3: adding a routing signal writes no change to the accumulation side."""

    def test_every_signal_lives_only_in_the_read_model(self) -> None:
        signals = [n for n in vars(read_model) if n.startswith("get_")]
        assert len(signals) >= 5
        for name in signals:
            assert name not in vars(storage), f"{name} is duplicated into storage"
            assert getattr(facade, name) is getattr(read_model, name)

    def test_every_accumulator_lives_only_in_storage(self) -> None:
        prefixes = ("_fold_", "_update_", "_merge_")
        accumulators = [n for n in vars(storage) if n.startswith(prefixes)]
        assert len(accumulators) >= 10
        for name in accumulators:
            assert name not in vars(read_model), f"{name} is duplicated into the read model"


class TestBenignLengthLeftIntact:
    """AC5: derivations were moved, not merged, to reduce a line count."""

    def test_each_derivation_survives_as_its_own_function(self) -> None:
        for name in (
            "get_dev_signal",
            "get_dev_success_rate",
            "get_review_signal",
            "get_role_reliability_signal",
            "get_dev_domain_signal",
            "get_dev_domain_complexity_signal",
            "get_observed_cost_tiebreak_signal",
            "get_dev_complexity_stats",
            "get_dev_score_cost_stats",
        ):
            assert callable(getattr(read_model, name))

    def test_the_merge_catalogue_survives_intact(self) -> None:
        for name in (
            "_merge_duration",
            "_merge_harness_terminated",
            "_merge_tainted_runs",
            "_merge_recent",
            "_merge_dev",
            "_merge_review",
            "_merge_completion",
            "_merge_cost_section",
            "_merge_preflight",
            "_merge_planner",
            "_merge_entry",
        ):
            assert callable(getattr(storage, name))
