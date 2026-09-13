# ruff: noqa: E501
"""GraphQL documents. Every field a tool reads appears in HEALTH_QUERY or FIELDS_USED so the
healthcheck notices a renamed field before a conversation does."""

ARRAY_DISK_FIELDS = (
    "id idx name device size status type temp isSpinning numErrors numReads numWrites "
    "fsSize fsFree fsUsed fsType warning critical rotational color"
)

SYSTEM_OVERVIEW = """
query SystemOverview {
  info {
    os { hostname uptime kernel release }
    cpu { brand cores threads }
    versions { core { unraid api kernel } }
  }
  metrics {
    cpu { percentTotal }
    memory { total used free available percentTotal }
  }
  array { state capacity { kilobytes { free used total } } }
  vars { shareMoverActive mdResync mdResyncPos mdResyncSize mdState }
}
"""

ARRAY_STATUS = f"""
query ArrayStatus {{
  array {{
    state
    capacity {{ kilobytes {{ free used total }} disks {{ free used total }} }}
    parityCheckStatus {{ status progress speed errors date duration running paused correcting }}
    parities {{ {ARRAY_DISK_FIELDS} }}
    disks {{ {ARRAY_DISK_FIELDS} }}
    caches {{ {ARRAY_DISK_FIELDS} }}
    boot {{ {ARRAY_DISK_FIELDS} }}
  }}
}}
"""

DISK_HEALTH_CACHED = f"""
query DiskHealthCached {{
  array {{
    parities {{ {ARRAY_DISK_FIELDS} }}
    disks {{ {ARRAY_DISK_FIELDS} }}
    caches {{ {ARRAY_DISK_FIELDS} }}
  }}
}}
"""

DISK_HEALTH_SMART = """
query DiskHealthSmart {
  disks { id name device serialNum vendor size type interfaceType temperature smartStatus isSpinning }
}
"""

UPS_STATUS = """
query UpsStatus {
  upsDevices {
    id name model status
    battery { chargeLevel estimatedRuntime health }
    power { inputVoltage outputVoltage loadPercentage nominalPower currentPower }
  }
}
"""

LIST_VMS = """
query ListVms { vms { domains { id name state } } }
"""

LIST_SHARES = """
query ListShares {
  shares { id name comment free used size cache include exclude allocator splitLevel floor luksStatus }
}
"""

NOTIFICATIONS = """
query Notifications($filter: NotificationFilter!) {
  notifications {
    overview { unread { info warning alert total } archive { info warning alert total } }
    list(filter: $filter) { id title subject description importance timestamp type }
  }
}
"""

MOVER_STATUS = """
query MoverStatus {
  vars { shareMoverActive shareMoverSchedule shareMoverLogging mdState }
  array { state }
}
"""

PARITY_CHECK = {
    "start": "mutation ParityStart($correct: Boolean!) { parityCheck { start(correct: $correct) } }",
    "pause": "mutation ParityPause { parityCheck { pause } }",
    "resume": "mutation ParityResume { parityCheck { resume } }",
    "cancel": "mutation ParityCancel { parityCheck { cancel } }",
}

VM_CONTROL = {
    "start": "mutation VmStart($id: PrefixedID!) { vm { start(id: $id) } }",
    "stop": "mutation VmStop($id: PrefixedID!) { vm { stop(id: $id) } }",
    "pause": "mutation VmPause($id: PrefixedID!) { vm { pause(id: $id) } }",
    "resume": "mutation VmResume($id: PrefixedID!) { vm { resume(id: $id) } }",
}

ARCHIVE_NOTIFICATION = """
mutation Archive($id: PrefixedID!) { archiveNotification(id: $id) { id type } }
"""

# One document touching every read field except the SMART `disks` query, which can spin
# disks up. Those fields are checked by introspection in FIELDS_USED.
HEALTH_QUERY = f"""
query ManifoldHealth {{
  info {{
    os {{ hostname uptime kernel release }}
    cpu {{ brand cores threads }}
    versions {{ core {{ unraid api kernel }} }}
  }}
  metrics {{ cpu {{ percentTotal }} memory {{ total used free available percentTotal }} }}
  array {{
    state
    capacity {{ kilobytes {{ free used total }} disks {{ free used total }} }}
    parityCheckStatus {{ status progress speed errors date duration running paused correcting }}
    parities {{ {ARRAY_DISK_FIELDS} }}
    disks {{ {ARRAY_DISK_FIELDS} }}
    caches {{ {ARRAY_DISK_FIELDS} }}
    boot {{ {ARRAY_DISK_FIELDS} }}
  }}
  vars {{ shareMoverActive shareMoverSchedule shareMoverLogging mdResync mdResyncPos mdResyncSize mdState }}
  vms {{ domains {{ id name state }} }}
  shares {{ id name comment free used size cache include exclude allocator splitLevel floor luksStatus }}
  notifications {{
    overview {{ unread {{ info warning alert total }} archive {{ info warning alert total }} }}
    list(filter: {{ type: UNREAD, offset: 0, limit: 1 }}) {{ id title subject description importance timestamp type }}
  }}
  upsDevices {{
    id name model status
    battery {{ chargeLevel estimatedRuntime health }}
    power {{ inputVoltage outputVoltage loadPercentage nominalPower currentPower }}
  }}
}}
"""

# One tiny query per root, used to name the roots a key cannot read when the combined
# health query is refused with a bare "Forbidden resource".
ROOT_PROBES = {
    "info": "query { info { os { hostname } } }",
    "metrics": "query { metrics { cpu { percentTotal } } }",
    "array": "query { array { state } }",
    "vars": "query { vars { mdState } }",
    "vms": "query { vms { domains { id } } }",
    "shares": "query { shares { id } }",
    "notifications": "query { notifications { overview { unread { total } } } }",
    "upsDevices": "query { upsDevices { id } }",
}

# Fields verified by introspection because executing them has side effects.
FIELDS_USED = {
    "Disk": [
        "id",
        "name",
        "device",
        "serialNum",
        "vendor",
        "size",
        "type",
        "interfaceType",
        "temperature",
        "smartStatus",
        "isSpinning",
    ],
    "ParityCheckMutations": ["start", "pause", "resume", "cancel"],
    "VmMutations": ["start", "stop", "pause", "resume"],
    "Mutation": ["archiveNotification"],
}

INTROSPECT_TYPE = """
query Introspect($name: String!) { __type(name: $name) { name fields { name } } }
"""
