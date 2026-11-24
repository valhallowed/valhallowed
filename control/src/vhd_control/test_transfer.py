import csv
import fcntl
import io
import itertools
import json
import os
import re
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

from vhd_control import cli, layout
from vhd_control.apply import MAKE_BACKUP_FOLDERS, Refused
from vhd_control.daemon import Daemon
from vhd_control.docker import DockerError, DockerTimeout, Ran
from vhd_control.reconcile import Restore
from vhd_control.status import StatusBook
from vhd_control.store import Store
from vhd_control.test_apply import IMAGES, FakeDocker, Setup
from vhd_control.test_daemon import ASHWAKE, MEADOWFOLD, REPO, SCHEMAS
from vhd_control.transfer import (
    IMPORT_FOLDER,
    MEMORY,
    PIDS,
    STOP_GRACE_S,
    TIMEOUT_S,
    WHO,
    Transfer,
)
from vhd_control.transfer import Refused as TransferRefused

BACKUP = "20261124T101500Z"
SAVE = {"number": 41, "objects": 1234, "written": "2026-11-18T19:40:11Z"}
HOST_INSTALL = "/srv/vhd"


def done(action: str, world: str = "Meadowfold", backup: str = BACKUP) -> str:
    result = {"contract": 1, "action": action, "world": world, "outcome": "done", "backup": backup, "save": SAVE}
    return json.dumps(result) + "\n"


def refused(action: str, reason: str, world: str = "Meadowfold") -> str:
    return json.dumps({"contract": 1, "action": action, "world": world, "outcome": "refused", "reason": reason}) + "\n"


def mounts(args: list[str]) -> dict[str, dict[str, str]]:
    """Each --mount, by its target: its fields, readonly as "true"."""
    found: dict[str, dict[str, str]] = {}
    for flag, value in itertools.pairwise(args):
        if flag != "--mount":
            continue
        fields: dict[str, str] = {}
        for field in next(csv.reader([value])):
            key, _, setting = field.partition("=")
            fields[key] = setting or "true"
        found[fields["target"]] = fields
    return found


class TransferDocker(FakeDocker):
    """FakeDocker, with the one-off containers vhd import and vhd export run."""

    def __init__(self, install: Path) -> None:
        super().__init__()
        self.install = install
        # The game runtime image's user: by default whoever runs the tests, so ownership can change.
        self.user = (os.getuid(), os.getgid())
        self.runs: list[tuple[list[str], float]] = []
        self.stopped: list[list[str]] = []
        # The stop's steps that fail, and whether the container is still there when one does.
        self.failing: set[str] = set()
        self.there = True
        self.listed: list[list[str]] = []
        self.on_stop: Callable[[list[str]], None] = lambda args: None
        # What the action's container does with its arguments: its exit status and output.
        self.act: Callable[[list[str]], tuple[int, str]] = self.done

    def output(self, args: list[str]) -> str:
        if args[0] == "run" and WHO in args:
            uid, gid = self.user
            return f"{uid}\n{gid}\n{gid}\n"
        if args[0] in ("stop", "rm"):
            self.stopped.append(args)
            self.on_stop(args)
            if args[0] in self.failing:
                raise DockerError(f"docker {args[0]}: the daemon did not answer")
            return ""
        if args[:2] == ["container", "ls"]:
            self.listed.append(args)
            return "0123456789ab\n" if self.there else ""
        return super().output(args)

    def run(self, args: list[str], timeout_s: float) -> Ran:
        self.runs.append((args, timeout_s))
        status, output = self.act(args)
        return Ran(status, output, ())

    def own_view(self, host_path: str) -> Path:
        """Where a host path under the install folder is in the tests' own view of it."""
        assert host_path.startswith(HOST_INSTALL + "/")
        return self.install / host_path.removeprefix(HOST_INSTALL + "/")

    def done(self, args: list[str]) -> tuple[int, str]:
        action, world = args[-2:] if args[-2] in ("import", "export") else args[-3:-1]
        if action == "export":
            saves = self.own_view(mounts(args)["/export"]["source"]) / "worlds_local" / world
            saves.mkdir(parents=True)
            (saves / "_main.41.ok").write_text("ok")
        return 0, done(action, world)


class Transfers(Setup):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.docker: TransferDocker = TransferDocker(self.install)
        save = self.install / "incoming" / "Meadowfold"
        save.mkdir(parents=True)
        (save / "_main.41.ok").write_text("ok")

    def transfer(self) -> Transfer:
        return Transfer(
            install=self.install,
            generations=self.volume,
            schemas=SCHEMAS,
            docker=self.docker,
            hostname="c0ffee",
            out=self.out,
        )

    def import_save(self, world: str = "Meadowfold", path: str = "incoming") -> None:
        self.transfer().import_save(world, path)

    def export_backup(self, world: str = "Meadowfold", backup: str | None = None) -> None:
        self.transfer().export_backup(world, backup)

    def restores(self) -> dict[str, Restore]:
        return Store(self.volume).load().restores


@pytest.fixture
def transfers(tmp_path: Path) -> Transfers:
    return Transfers(tmp_path)


def test_an_import_runs_the_runtime_image_sandboxed_with_the_save_read_only(transfers: Transfers) -> None:
    transfers.import_save()

    [(args, timeout)] = transfers.docker.runs
    assert timeout == TIMEOUT_S
    name = args[args.index("--name") + 1]
    assert re.fullmatch(r"valhallowed-import-[0-9a-f]{16}", name)
    assert args[:2] == ["run", "--rm"]
    for flag in (
        ["--pull", "never"],
        ["--network", "none"],
        ["--read-only"],
        ["--cap-drop=ALL"],
        ["--security-opt=no-new-privileges:true"],
        ["--memory", MEMORY],
        ["--pids-limit", str(PIDS)],
    ):
        assert any(args[i : i + len(flag)] == flag for i in range(len(args))), flag
    # The image's own user, and nothing that could widen the sandbox.
    for widening in ("--user", "--privileged", "--cap-add", "--volume", "--tmpfs", "--entrypoint"):
        assert not any(arg.startswith(widening) for arg in args), widening
    assert mounts(args) == {
        str(IMPORT_FOLDER): {
            "type": "bind",
            "source": f"{HOST_INSTALL}/incoming",
            "target": "/import",
            "readonly": "true",
        },
        "/backups": {"type": "bind", "source": f"{HOST_INSTALL}/backups/{MEADOWFOLD}", "target": "/backups"},
    }
    assert args[-3:] == [IMAGES["game-runtime"], "import", "Meadowfold"]


def test_an_import_makes_only_its_worlds_backups_folder_first(transfers: Transfers) -> None:
    transfers.import_save()
    [made] = [args for args in transfers.docker.prepared if MAKE_BACKUP_FOLDERS in args]
    assert made[-2:] == ["sh", MEADOWFOLD]
    assert made[made.index("--volume") + 1] == f"{HOST_INSTALL}/backups:{layout.BACKUPS}"


def test_a_backups_folder_that_cannot_be_made_refuses_the_import_before_it_runs(transfers: Transfers) -> None:
    transfers.docker.fail_backups = True
    with pytest.raises(TransferRefused, match="could not make Meadowfold's folder in /srv/vhd/backups"):
        transfers.import_save()
    assert transfers.docker.runs == []


def test_an_import_asks_for_the_restore_of_its_backup(transfers: Transfers) -> None:
    transfers.apply()
    transfers.import_save()

    restore = transfers.restores()[MEADOWFOLD]
    assert restore.backup == BACKUP
    assert re.fullmatch(r"[0-9a-f]{32}", restore.id)
    assert ASHWAKE not in transfers.restores()
    out = transfers.out.getvalue()
    assert (
        f"vhd import: Meadowfold's save 41 (1234 objects, completed 2026-11-18T19:40:11Z) is kept as backup {BACKUP}, "
        f"and restore {restore.id} of it is asked for. The world's runtime restores it once no players are online"
    ) in out


def test_a_world_before_its_first_apply_loads_the_import_in_its_first_generation(transfers: Transfers) -> None:
    transfers.import_save()
    assert "The world has no container yet: vhd apply starts it" in transfers.out.getvalue()
    assert Store(transfers.volume).load().generation is None

    transfers.apply()

    restore = transfers.restores()[MEADOWFOLD]
    assert transfers.document(MEADOWFOLD)["generation"] == 1
    assert transfers.document(MEADOWFOLD)["restore"] == {"id": restore.id, "backup": BACKUP}
    assert "restore" not in transfers.document(ASHWAKE)


def daemon_over(transfers: Transfers, state: str = "running", **report: object) -> tuple[Daemon, StatusBook]:
    book = StatusBook()
    book.record({"contract": 1, "generation": 1, "world": "Meadowfold", "state": state, **report})
    return Daemon(transfers.install, Store(transfers.volume), SCHEMAS, book), book


def test_the_running_control_plane_hands_the_imports_restore_at_its_next_pass(transfers: Transfers) -> None:
    transfers.apply()
    control, _ = daemon_over(transfers, players=2)
    control.step()
    transfers.import_save()

    control.step()

    restore = transfers.restores()[MEADOWFOLD]
    assert transfers.document(MEADOWFOLD)["restore"] == {"id": restore.id, "backup": BACKUP}


def test_a_held_world_takes_the_imports_restore(transfers: Transfers) -> None:
    transfers.apply()
    transfers.docker.running = [MEADOWFOLD, ASHWAKE]
    held = {"contract": 1, "generation": 1, "world": "Meadowfold", "state": "held", "reason": "no save to load"}
    assert transfers.worlds is not None
    transfers.worlds["Meadowfold"] = {"published": 1, "state": "held", "report": held, "report_age_s": 1}
    transfers.import_save()
    restore = {"id": transfers.restores()[MEADOWFOLD].id, "backup": BACKUP}

    control, _ = daemon_over(transfers, state="held", reason="no save to load")
    control.step()
    assert transfers.document(MEADOWFOLD)["restore"] == restore

    # vhd apply too, without --disconnect-players: a held world has no players.
    transfers.apply()
    assert transfers.document(MEADOWFOLD)["restore"] == restore
    assert "disconnect_players" not in transfers.document(MEADOWFOLD)


def test_an_import_holds_vhd_applys_lock(transfers: Transfers) -> None:
    with (transfers.volume / ".apply.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(Refused, match="another vhd apply, vhd import or vhd export is running"):
            transfers.import_save()
    assert transfers.docker.runs == []

    seen: list[bool] = []

    def try_lock(args: list[str]) -> tuple[int, str]:
        with (transfers.volume / ".apply.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                seen.append(True)
        return 0, done("import")

    transfers.docker.act = try_lock
    transfers.import_save()
    assert seen == [True]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/srv/vhd/incoming", "is an absolute path"),
        ("incoming/../incoming", "holds \\.\\."),
        ("", "not the folder itself"),
        (".", "not the folder itself"),
        ("elsewhere", "the install folder holds no elsewhere"),
        ("manifest.toml", "manifest.toml in the install folder is no folder"),
        ("backups/old", "is in the backups folder"),
    ],
)
def test_an_import_refuses_a_folder_outside_or_unfit_before_anything_runs(
    transfers: Transfers, path: str, reason: str
) -> None:
    (transfers.install / "backups" / "old").mkdir(parents=True)
    with pytest.raises(TransferRefused, match=reason):
        transfers.import_save(path=path)
    assert transfers.docker.runs == []
    assert transfers.docker.prepared == []


def test_an_import_refuses_a_link_at_any_part_of_its_path(transfers: Transfers) -> None:
    (transfers.install / "linked").symlink_to(transfers.install / "incoming")
    (transfers.install / "deeper").mkdir()
    (transfers.install / "deeper" / "save").symlink_to(transfers.install / "incoming")
    for path in ("linked", "deeper/save", "linked/Meadowfold"):
        with pytest.raises(TransferRefused, match="is a link"):
            transfers.import_save(path=path)
    assert transfers.docker.runs == []


def test_an_import_refuses_a_world_the_manifest_does_not_have(transfers: Transfers) -> None:
    with pytest.raises(TransferRefused, match="the manifest has no world Hollowmere"):
        transfers.import_save(world="Hollowmere")
    assert transfers.docker.runs == []


def test_an_import_refuses_a_save_the_runtimes_user_cannot_read_and_says_how_to_fix_it(transfers: Transfers) -> None:
    owner = (transfers.install / "incoming").stat()
    # Another user, in another group: only the bits for others count.
    transfers.docker.user = (owner.st_uid + 4242, owner.st_gid + 4242)
    ok = transfers.install / "incoming" / "Meadowfold" / "_main.41.ok"
    ok.chmod(0o640)
    with pytest.raises(
        TransferRefused,
        match=r"cannot read incoming/Meadowfold/_main\.41\.ok; make the folder and everything in it readable by "
        r"others, as: chmod -R o\+rX incoming",
    ):
        transfers.import_save()
    assert transfers.docker.runs == []

    ok.chmod(0o644)
    (transfers.install / "incoming" / "Meadowfold").chmod(0o750)
    with pytest.raises(TransferRefused, match="cannot read incoming/Meadowfold;"):
        transfers.import_save()

    (transfers.install / "incoming" / "Meadowfold").chmod(0o755)
    transfers.import_save()
    assert len(transfers.docker.runs) == 1


def test_a_save_readable_by_the_runtimes_group_is_read(transfers: Transfers) -> None:
    owner = (transfers.install / "incoming").stat()
    transfers.docker.user = (owner.st_uid + 4242, owner.st_gid)
    for each in (transfers.install / "incoming", transfers.install / "incoming" / "Meadowfold"):
        each.chmod(0o750)
    (transfers.install / "incoming" / "Meadowfold" / "_main.41.ok").chmod(0o640)
    transfers.import_save()
    assert len(transfers.docker.runs) == 1


def test_a_path_any_character_may_hold_reaches_docker_whole(transfers: Transfers) -> None:
    odd = transfers.install / 'in,co"ming'
    (transfers.install / "incoming").rename(odd)
    transfers.import_save(path='in,co"ming')
    [(args, _)] = transfers.docker.runs
    assert mounts(args)["/import"]["source"] == f'{HOST_INSTALL}/in,co"ming'


@pytest.mark.parametrize(
    ("status", "output", "reason", "kept"),
    [
        (
            1,
            refused("import", "the save folder holds no complete save: no _main.<n>.ok"),
            "^the save folder holds no",
            False,
        ),
        (0, refused("import", "a reason\u0085with a control"), "a reason\u0085with a control", False),
        (1, "", "gave vhd import no result, and exited with 1", True),
        (1, "not json\n", "a result that is no JSON", True),
        (
            0,
            json.dumps({"contract": 1, "action": "import", "world": "Meadowfold", "outcome": "done"}),
            "breaks its contract",
            True,
        ),
        (0, done("import", world="Ashwake"), "is for import of Ashwake", True),
        (0, done("export"), "is for export of Meadowfold", True),
        (1, done("import"), "said vhd import was done, but exited with 1", True),
    ],
)
def test_an_import_whose_result_is_no_done_asks_for_no_restore(
    transfers: Transfers, status: int, output: str, reason: str, kept: bool
) -> None:
    transfers.docker.act = lambda args: (status, output)
    with pytest.raises(TransferRefused, match=reason) as refusal:
        transfers.import_save()
    assert transfers.restores() == {}
    # Output that breaks the contract, or none, may come from an import that kept its backup (B13).
    warned = "an import cut short after it named its backup may have kept it, which vhd backups then lists"
    assert str(refusal.value).endswith(warned) == kept


def test_an_import_that_outlasts_its_timeout_is_stopped_with_its_container(transfers: Transfers) -> None:
    def hang(args: list[str]) -> tuple[int, str]:
        raise DockerTimeout("docker run did not finish")

    transfers.docker.act = hang
    with pytest.raises(TransferRefused, match=f"did not finish within {TIMEOUT_S} s, so vhd stopped it; an import"):
        transfers.import_save()
    [(args, _)] = transfers.docker.runs
    name = args[args.index("--name") + 1]
    assert transfers.docker.stopped == [["stop", "--timeout", str(STOP_GRACE_S), name], ["rm", "--force", name]]
    assert transfers.restores() == {}


def hang(args: list[str]) -> tuple[int, str]:
    raise DockerTimeout("docker run did not finish")


def test_a_container_already_gone_is_no_failure_of_the_stop(transfers: Transfers) -> None:
    transfers.docker.act = hang
    transfers.docker.failing = {"stop", "rm"}
    transfers.docker.there = False
    with pytest.raises(TransferRefused, match="so vhd stopped it; an import cut short after") as refused:
        transfers.import_save()
    assert "failed too" not in str(refused.value)
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]
    # Gone by Docker's own list of containers by that exact name, not by its words.
    name = transfers.docker.stopped[0][-1]
    assert transfers.docker.listed[0] == ["container", "ls", "--all", "--quiet", "--filter", f"name=^{name}$"]


def test_a_failed_stop_still_removes_the_container_and_the_refusal_keeps_the_timeout(transfers: Transfers) -> None:
    transfers.docker.act = hang
    transfers.docker.failing = {"stop"}
    with pytest.raises(
        TransferRefused,
        match=f"did not finish within {TIMEOUT_S} s, so vhd stopped it; .*; stopping its container failed too: "
        "docker stop: the daemon did not answer$",
    ):
        transfers.import_save()
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]


def test_an_interrupted_import_whose_stop_fails_is_still_an_interrupt(transfers: Transfers) -> None:
    def interrupt(args: list[str]) -> tuple[int, str]:
        raise KeyboardInterrupt

    transfers.docker.act = interrupt
    transfers.docker.failing = {"stop", "rm"}
    with pytest.raises(KeyboardInterrupt):
        transfers.import_save()
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]


def test_an_interrupted_import_stops_its_container_too(transfers: Transfers) -> None:
    def interrupt(args: list[str]) -> tuple[int, str]:
        raise KeyboardInterrupt

    transfers.docker.act = interrupt
    with pytest.raises(KeyboardInterrupt):
        transfers.import_save()
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]
    assert transfers.restores() == {}


def test_a_vhd_stopped_by_sigterm_stops_its_actions_container_and_removes_what_it_made(transfers: Transfers) -> None:
    before = signal.getsignal(signal.SIGTERM)

    def terminated(args: list[str]) -> tuple[int, str]:
        os.kill(os.getpid(), signal.SIGTERM)
        return transfers.docker.done(args)

    transfers.docker.act = terminated
    with pytest.raises(KeyboardInterrupt):
        transfers.export_backup()
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]
    assert not (transfers.install / "exports").exists()
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_second_ctrl_c_while_the_container_stops_still_removes_it(transfers: Transfers) -> None:
    transfers.docker.act = hang
    transfers.docker.on_stop = lambda args: os.kill(os.getpid(), signal.SIGINT) if args[0] == "stop" else None
    with pytest.raises(KeyboardInterrupt):
        transfers.import_save()
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]


def test_the_timeout_is_well_within_the_age_after_which_an_imports_leftovers_are_removed() -> None:
    backups = (REPO / "runtime/src/vhd_runtime/backups.py").read_text()
    match = re.search(r"^IMPORT_STAGING_MAX_AGE_S = (\d+)$", backups, re.MULTILINE)
    assert match is not None
    assert TIMEOUT_S <= int(match[1]) // 2


def test_an_export_copies_the_newest_backup_into_a_folder_of_its_own_for_the_install_folders_owner(
    transfers: Transfers,
) -> None:
    owner = transfers.install.stat()
    transfers.export_backup()

    [(args, timeout)] = transfers.docker.runs
    assert timeout == TIMEOUT_S
    assert args[-3:] == [IMAGES["game-runtime"], "export", "Meadowfold"]
    assert (
        "--read-only" in args and ["--network", "none"] == args[args.index("--network") : args.index("--network") + 2]
    )
    found = mounts(args)
    assert found["/backups"] == {
        "type": "bind",
        "source": f"{HOST_INSTALL}/backups/{MEADOWFOLD}",
        "target": "/backups",
        "readonly": "true",
    }
    target = transfers.docker.own_view(found["/export"]["source"])
    assert "readonly" not in found["/export"]
    assert target.parent == transfers.install / "exports" / "Meadowfold"
    assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", target.name)
    for path in (target, *target.rglob("*")):
        assert (path.stat().st_uid, path.stat().st_gid) == (owner.st_uid, owner.st_gid)
    assert (target / "worlds_local" / "Meadowfold" / "_main.41.ok").exists()
    shown = f"exports/Meadowfold/{target.name}/worlds_local/Meadowfold"
    assert f"vhd export: Meadowfold's backup {BACKUP}, save 41 (1234 objects" in transfers.out.getvalue()
    assert f"is in {shown}, in the layout the game reads" in transfers.out.getvalue()
    # No world's backups folder is made for an export: a missing one is the action's refusal.
    assert not any(MAKE_BACKUP_FOLDERS in args for args in transfers.docker.prepared)


def test_an_export_names_the_backup_it_was_asked_for_and_two_never_share_a_folder(transfers: Transfers) -> None:
    transfers.export_backup(backup=BACKUP)
    transfers.export_backup(backup=BACKUP)
    [(first, _), (second, _)] = transfers.docker.runs
    assert first[-4:] == [IMAGES["game-runtime"], "export", "Meadowfold", BACKUP]
    assert mounts(first)["/export"]["source"] != mounts(second)["/export"]["source"]
    assert len(list((transfers.install / "exports" / "Meadowfold").iterdir())) == 2


def test_the_export_folder_is_the_runtime_users_while_the_container_writes_it(transfers: Transfers) -> None:
    owners: list[tuple[int, int]] = []

    def look(args: list[str]) -> tuple[int, str]:
        info = transfers.docker.own_view(mounts(args)["/export"]["source"]).stat()
        owners.append((info.st_uid, info.st_gid))
        return transfers.docker.done(args)

    transfers.docker.act = look
    transfers.export_backup()
    assert owners == [transfers.docker.user]


def test_a_refused_export_removes_what_vhd_made(transfers: Transfers) -> None:
    transfers.docker.act = lambda args: (
        1,
        refused("export", "the world's backups folder holds no backup of Meadowfold"),
    )
    with pytest.raises(TransferRefused, match="holds no backup of Meadowfold"):
        transfers.export_backup()
    assert not (transfers.install / "exports").exists()

    (transfers.install / "exports" / "Meadowfold" / "earlier").mkdir(parents=True)
    with pytest.raises(TransferRefused):
        transfers.export_backup()
    assert [p.name for p in (transfers.install / "exports" / "Meadowfold").iterdir()] == ["earlier"]


def test_an_export_refuses_an_exports_folder_that_is_a_link(transfers: Transfers, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (transfers.install / "exports").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(TransferRefused, match="exports in the install folder is no folder"):
        transfers.export_backup()
    assert transfers.docker.runs == []
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_an_export_that_outlasts_its_timeout_is_stopped_and_leaves_nothing(transfers: Transfers) -> None:
    def hang(args: list[str]) -> tuple[int, str]:
        raise DockerTimeout("docker run did not finish")

    transfers.docker.act = hang
    with pytest.raises(TransferRefused, match=f"vhd export did not finish within {TIMEOUT_S} s, so vhd stopped it$"):
        transfers.export_backup()
    assert [args[0] for args in transfers.docker.stopped] == ["stop", "rm"]
    assert not (transfers.install / "exports").exists()


def test_an_export_holds_vhd_applys_lock(transfers: Transfers) -> None:
    with (transfers.volume / ".apply.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(Refused, match="another vhd apply, vhd import or vhd export is running"):
            transfers.export_backup()
    assert transfers.docker.runs == []


def test_a_refused_import_fails_the_command_with_its_reason_as_printable_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(self: Transfer, world: str, path: str) -> None:
        raise TransferRefused("the save holds _main.41.ok\u0085, which is no file of a save")

    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    monkeypatch.setattr(Transfer, "import_save", refuse)
    out = io.StringIO()
    assert cli.main(["import", "Meadowfold", "incoming"], out) == 1
    assert out.getvalue() == ""
    assert "vhd import: the save holds _main.41.ok?, which is no file of a save" in capsys.readouterr().err


def test_export_takes_the_backup_as_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[str, str | None]] = []

    def export(self: Transfer, world: str, backup: str | None) -> None:
        asked.append((world, backup))

    monkeypatch.setattr(layout, "SCHEMAS", REPO)
    monkeypatch.setattr(Transfer, "export_backup", export)
    assert cli.main(["export", "Meadowfold"], io.StringIO()) == 0
    assert cli.main(["export", "Meadowfold", BACKUP], io.StringIO()) == 0
    assert asked == [("Meadowfold", None), ("Meadowfold", BACKUP)]
