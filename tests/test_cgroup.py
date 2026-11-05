from bench.cgroup import MIB, Cgroup, Sampler, snapshot


def make(tmp_path, current=100, peak=None, events="oom 0\noom_kill 0\n", cpu=None, max_="536870912"):
    (tmp_path / "memory.current").write_text(str(current * MIB))
    if peak is not None:
        (tmp_path / "memory.peak").write_text(str(peak * MIB))
    (tmp_path / "memory.events").write_text(events)
    (tmp_path / "memory.max").write_text(max_)
    (tmp_path / "cpu.stat").write_text(cpu or "usage_usec 2000000\nnr_periods 100\nnr_throttled 25\nthrottled_usec 1500000\n")
    return Cgroup(tmp_path)


def test_snapshot_units(tmp_path):
    s = snapshot(make(tmp_path, peak=480, events="oom 1\noom_kill 2\n"))
    assert s["peak_mem_mib"] == 480 and s["mem_limit_mib"] == 512 and s["oom_kills_inside"] == 2
    assert s["cpu_throttled_pct"] == 25.0 and s["cpu_throttled_s"] == 1.5 and s["cgroup_available"]


def test_peak_falls_back_to_sampling(tmp_path):
    cg = make(tmp_path, current=200)
    sm = Sampler(cg, interval_s=0.01)
    sm.max_current = 300 * MIB
    assert sm.peak_bytes() == (300 * MIB, "sampled")
    assert snapshot(cg, sm)["peak_mem_source"] == "sampled"


def test_unavailable_outside_a_container(tmp_path):
    s = snapshot(Cgroup(tmp_path / "nope"))
    assert s["cgroup_available"] is False and s["peak_mem_mib"] is None and s["oom_kills_inside"] == 0


def test_max_means_unlimited(tmp_path):
    assert make(tmp_path, max_="max").memory_max() is None


def make_v1(tmp_path, usage=100, peak=480, limit=536870912, oom_kill=0, throttled=25):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "memory.usage_in_bytes").write_text(str(usage * MIB))
    (mem / "memory.max_usage_in_bytes").write_text(str(peak * MIB))
    (mem / "memory.limit_in_bytes").write_text(str(limit))
    (mem / "memory.oom_control").write_text(f"oom_kill_disable 0\nunder_oom 0\noom_kill {oom_kill}\n")
    cpu = tmp_path / "cpu,cpuacct"
    cpu.mkdir()
    (cpu / "cpu.stat").write_text(f"nr_periods 100\nnr_throttled {throttled}\nthrottled_time 1500000000\n")
    (cpu / "cpuacct.usage").write_text("2000000000")
    return Cgroup(tmp_path)


def test_cgroup_v1_layout_is_read_like_v2(tmp_path):
    cg = make_v1(tmp_path, oom_kill=3)
    assert cg.version == 1 and cg.available
    s = snapshot(cg)
    assert s["cgroup_version"] == 1
    assert s["peak_mem_mib"] == 480 and s["mem_limit_mib"] == 512 and s["oom_kills_inside"] == 3
    assert s["cpu_throttled_pct"] == 25.0 and s["cpu_throttled_s"] == 1.5 and s["cpu_usage_s"] == 2.0


def test_v1_unlimited_memory_is_none(tmp_path):
    assert make_v1(tmp_path, limit=9223372036854771712).memory_max() is None


def test_v2_is_preferred_when_both_exist(tmp_path):
    make_v1(tmp_path)
    make(tmp_path, peak=200)
    assert Cgroup(tmp_path).version == 2
