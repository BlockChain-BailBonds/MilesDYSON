"""Task-independent form of DYSON v10's observed-evidence mode controller."""
from __future__ import annotations

from collections import OrderedDict, deque


class Session:
    def __init__(self, config):
        self.outcomes = deque(maxlen=config["evidence_window"])
        self.latencies = deque(maxlen=12)
        self.mode = "BASELINE"
        self.requests = 0
        self.noop_streak = 0
        self.last_switch = -1000000
        self.level = None
        self.unknown = 0


class Equalizer:
    """Only explicit observed outcomes count as progress or no-op evidence."""

    def __init__(self, config):
        self.config = dict(config)
        self.sessions = OrderedDict()

    def state(self, session_id):
        if session_id not in self.sessions:
            if len(self.sessions) >= 256:
                self.sessions.popitem(last=False)
            self.sessions[session_id] = Session(self.config)
        self.sessions.move_to_end(session_id)
        return self.sessions[session_id]

    def parameters(self, session_id):
        s = self.state(session_id)
        c = self.config
        tokens = c["max_tokens"]
        if c["equalizer_enabled"] and s.mode in {"MOMENTUM", "ECONOMY"}:
            gain = c["progress_gain"] if s.mode == "MOMENTUM" else c["economy_gain"]
            tokens = min(tokens, max(c["min_tokens"], round(tokens * gain)))
        return {"temperature": c["temperature"], "top_p": c["top_p"],
                "max_tokens": tokens, "seed": c["seed"]}

    def response(self, session_id, elapsed):
        s = self.state(session_id)
        s.requests += 1
        s.latencies.append(elapsed)

    def observe(self, session_id, changed, completed=False, level=None):
        s = self.state(session_id)
        c = self.config
        if level is not None and level != s.level:
            s.level = level
            s.outcomes.clear()
            s.noop_streak = 0
            s.mode = "BASELINE"
            s.last_switch = s.requests - c["cooldown_requests"]
        if changed is True or completed:
            s.outcomes.append(1)
            s.noop_streak = 0
        elif changed is False:
            s.outcomes.append(0)
            s.noop_streak += 1
        else:
            s.unknown += 1
        if not c["equalizer_enabled"] or len(s.outcomes) < c["min_observations"]:
            return None
        if s.requests - s.last_switch < c["cooldown_requests"]:
            return None
        rate = sum(s.outcomes) / len(s.outcomes)
        recent = list(s.latencies)[-4:]
        slow = len(recent) >= 3 and sum(recent) / len(recent) >= c["latency_target_s"]
        if s.noop_streak >= c["stall_streak"] and rate <= 0.35:
            mode, reason = "RECOVERY", "observed_stall"
        elif rate >= 0.70 and sum(s.outcomes) >= 3:
            mode, reason = "MOMENTUM", "observed_progress"
        elif slow and rate < 0.70:
            mode, reason = "ECONOMY", "measured_high_latency"
        else:
            mode, reason = "BASELINE", "inconclusive_evidence"
        if mode == s.mode:
            return None
        before, s.mode = s.mode, mode
        s.last_switch = s.requests
        return {"before": before, "after": mode, "reason": reason,
                "known_outcomes": len(s.outcomes), "change_rate": rate}

    def snapshot(self):
        return {key: {"mode": s.mode, "requests": s.requests,
                      "known_outcomes": len(s.outcomes), "unknown_outcomes": s.unknown,
                      "noop_streak": s.noop_streak,
                      "mean_latency_s": sum(s.latencies) / len(s.latencies) if s.latencies else None}
                for key, s in self.sessions.items()}
