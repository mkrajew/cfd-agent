"""Consistency of the VM files, cloud-init and Bicep. Pure text checks: no bash, no Azure."""

import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2] / "infra"
VM_DIR = INFRA / "vm"
CLOUD_INIT = (VM_DIR / "cloud-init.yaml").read_text(encoding="utf-8")
VM_BICEP = (INFRA / "modules" / "vm.bicep").read_text(encoding="utf-8")
ROLES_BICEP = (INFRA / "modules" / "roles.bicep").read_text(encoding="utf-8")


def test_every_cloud_init_placeholder_is_filled_by_vm_bicep_and_vice_versa():
    in_cloud_init = set(re.findall(r"__[A-Z0-9_]+__", CLOUD_INIT))
    in_cloud_init.discard("__PLACEHOLDERS__")  # named in the header comment
    filled = set(re.findall(r"placeholder: '(__[A-Z0-9_]+__)'", VM_BICEP))
    assert in_cloud_init == filled


def test_every_file_vm_bicep_loads_exists():
    for name in re.findall(r"loadTextContent\('\.\./vm/([^']+)'\)", VM_BICEP):
        assert (VM_DIR / name).is_file(), name


def test_cloud_init_installs_the_firewall_ssh_drop_in_and_daily_deferral():
    assert "/etc/ssh/sshd_config.d/20-cfdagent-ssh-extra.conf" in CLOUD_INIT
    assert "/usr/local/sbin/cfdagent-iptables" in CLOUD_INIT
    assert "/etc/systemd/system/cfdagent-iptables.service" in CLOUD_INIT
    assert "systemctl, enable, --now, cfdagent-iptables.service" in CLOUD_INIT
    assert "cfdagent-daily-shutdown.timer" in CLOUD_INIT


def test_the_daily_service_defers_and_the_timer_retries_every_ten_minutes():
    service = (VM_DIR / "daily-shutdown.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/sbin/cfdagent-idle-shutdown --daily" in service
    timer = (VM_DIR / "daily-shutdown.timer").read_text(encoding="utf-8")
    assert timer.count("__ON_CALENDAR__") == 1, "a second mention would be replaced as well"
    assert "OnUnitActiveSec=10min" in timer
    assert len(re.findall(r"^OnCalendar=", timer, re.MULTILINE)) == 1
    assert "range(" not in VM_BICEP, "the timer needs no generated list of attempts"


def test_firewall_unit_is_ordered_after_docker_and_removes_its_rules_on_stop():
    unit = (VM_DIR / "cfdagent-iptables.service").read_text(encoding="utf-8")
    for line in (
        "After=docker.service",
        "Requires=docker.service",
        "Type=oneshot",
        "RemainAfterExit=yes",
        "ExecStart=/usr/local/sbin/cfdagent-iptables start",
        "ExecStop=/usr/local/sbin/cfdagent-iptables stop",
        "WantedBy=multi-user.target",
    ):
        assert line in unit


def test_ssh_template_has_the_limits_and_one_placeholder():
    text = (VM_DIR / "20-cfdagent-ssh-extra.conf").read_text(encoding="utf-8")
    directives = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert directives == ["MaxAuthTries 3", "AllowUsers __ADMIN_USER__"]


def test_vm_identity_writes_blobs_only_in_the_runs_container():
    block = ROLES_BICEP[ROLES_BICEP.index("resource vmBlob") :]
    block = block[: block.index("\n}\n")]
    assert "scope: runsContainer" in block
    assert "storageBlobDataContributor" in block
    assert "scope: storage" not in block
    assert "name: 'runs'" in ROLES_BICEP
    # The owner keeps read access to the whole account.
    assert "scope: storage" in ROLES_BICEP[ROLES_BICEP.index("resource ownerBlobReader") :]
