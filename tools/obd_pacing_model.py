#!/usr/bin/env python3
"""What response pacing would buy, as a function of what a car actually does.

`proposals/obd-response-paced-polling.md` §7 asks the question this answers:
before designing around a fixed poll clock being wasteful, put a number on
the waste. The reference peripheral cannot supply it -- its ECUs answer in
the same tick -- so this models the two pacing rules directly. They are
simple enough that the model is exact rather than approximate.

Definitions, all milliseconds:

  F   obd_min_interval_ms, the device's declared floor (SPEC.md 15.4)
  I   interval_ms, what the client installs
  L   the car's request->response latency
  g   groups in the schedule (SPEC.md 15.4.1)

SPEC.md 15.4 today -- the fixed clock. One request per I, whatever the car
does, and a request unanswered when the next is due is abandoned:

  spacing = I

The brief's §3.1 rule -- response-paced, floored, with I as a ceiling:

  next_tx = min(max(t + F, first_response_after(t)), t + I)
  spacing = min(max(F, L), I)     ==  clamp(L, F, I)

so the whole of what pacing buys, at one interval, is

  gain = I / clamp(L, F, I)

and that expression is the finding: it is bounded above by I/F, it does not
depend on L at all once L <= F, and it is exactly 1 when I == F.
"""
import argparse


def spacing_fixed(F, I, L):
    """SPEC.md 15.4 as it stands: one request per interval, regardless."""
    return I


def spacing_paced(F, I, L):
    """The brief's §3.1: answered-or-ceiling, never below the floor."""
    return min(max(F, L), I)


def gain(F, I, L):
    return spacing_fixed(F, I, L) / spacing_paced(F, I, L)


def stale(F, I, L):
    """Is the answer to a request still outstanding when the next goes out?

    Under BOTH rules a request unanswered at t+I is abandoned (SPEC.md 15.1),
    so this is a property of I against L and pacing does not change it. It is
    reported because the brief's §1 calls it the serious half of the problem,
    and the model says pacing does not touch it.
    """
    return L >= I


def table(F, intervals, latencies):
    print(f"\n  floor F = {F} ms\n")
    head = "  " + "L (ms)".ljust(9) + "".join(f"I={i:<10}" for i in intervals)
    print(head)
    print("  " + "-" * (len(head) - 2))
    for L in latencies:
        row = f"  {L:<9}"
        for I in intervals:
            g = gain(F, I, L)
            mark = "!" if stale(F, I, L) else " "
            row += f"{g:>5.2f}x{mark}    "
        print(row)
    print("\n  ! = the car is slower than the interval, so requests are")
    print("      abandoned under BOTH rules (SPEC.md 15.1). Pacing does not")
    print("      change this column; only lowering the rate does.")


def percentile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def informed_comparison(F, samples, label, drop_budget=0.01):
    """Pacing against a client that KNOWS the latency, not one guessing.

    The table above measures pacing against whatever interval a client
    happened to pick, which flatters it: the gain is I/F, and I is the
    client's own choice. The honest comparison is against a client told what
    the car does -- open question 5.2 in the brief -- which sets I just above
    the latency it must tolerate and captures the same gain with no change to
    the transmit loop.

    What a latency REPORT cannot replicate is variance. A client tuning one
    static interval must cover the tail, and pays the tail on every sample.
    Pacing pays the ACTUAL latency each time. So this is the whole remaining
    argument for pacing, and its size is a property of the distribution:

        advantage = tail_interval / mean(clamp(L, F, tail_interval))
    """
    tail = percentile(samples, 1 - drop_budget)
    informed_I = max(F, tail)
    paced = sum(min(max(F, L), informed_I) for L in samples) / len(samples)
    blind_I = max(F, 25)
    print(f"  {label:<34} mean L {sum(samples)/len(samples):5.1f}  "
          f"p99 {tail:5.1f}  "
          f"blind I={blind_I:<4} -> {blind_I / (sum(min(max(F, L), blind_I) for L in samples) / len(samples)):5.2f}x  "
          f"informed I={informed_I:<4} -> {informed_I / paced:5.2f}x")


def from_session_csv(path, column="deviceCapturedAtUs"):
    """Latency samples from a LapSmith session's obd.csv.

    LapSmith's ELM327 loop is response-paced -- it awaits each round trip and
    issues the next immediately -- so the gaps between consecutive DISTINCT
    capture instants are round-trip times. The file is resampled to a fixed
    rate, hence the de-duplication: several rows carry one capture.

    What comes out is SWEEP-level, not per-request: a sweep may carry several
    PIDs, and LapSmith's tiering makes some sweeps longer by design. Divide
    by requests-per-sweep for absolute latency. The ratio the decision turns
    on is invariant under that divisor, which is why it is not a parameter.
    """
    import csv
    with open(path) as fh:
        instants = sorted({int(r[column]) for r in csv.DictReader(fh)
                           if r.get(column)})
    return [(b - a) / 1000.0 for a, b in zip(instants, instants[1:]) if b > a]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-session", metavar="obd.csv",
                    help="measure a real distribution from a LapSmith "
                         "session's obd.csv instead of modelling shapes")
    ap.add_argument("--floor", type=int, default=20,
                    help="obd_min_interval_ms (reference peripheral: 20)")
    args = ap.parse_args()

    if args.from_session:
        samples = from_session_csv(args.from_session)
        print(f"\n  {len(samples)} intervals measured from "
              f"{args.from_session}\n")
        informed_comparison(args.floor, samples, "measured (real car)")
        med = percentile(samples, 0.5)
        print(f"\n  tail/median: p95 {percentile(samples, .95) / med:.1f}x   "
              f"p99 {percentile(samples, .99) / med:.1f}x")
        print("  Decision rule (proposal §4.3): tail ~ median -> decline and "
              "report\n  the latency instead; tail >> median -> accept, no "
              "static interval serves both.")
        return

    intervals = [args.floor, 25, 50, 100]
    latencies = [1, 3, 5, 10, 18, 25, 50]

    print(__doc__.split("Definitions")[0].rstrip())
    table(args.floor, intervals, latencies)

    print("\n  The same, for a device declaring a 5 ms floor:")
    table(5, [5, 25, 50, 100], latencies)

    print("\n  Pacing vs a client that KNOWS the latency (the real question).")
    print("  'blind' = a client picking 25 ms with no information, which is")
    print("  what LapSmith does today. 'informed' = one told the distribution")
    print("  and setting the interval to the p99 it must tolerate.\n")
    # Deterministic shapes rather than sampled: no RNG, so the numbers are
    # reproducible and the argument does not rest on a seed.
    shapes = {
        "tight fast car (3 +/- 1 ms)": [2, 3, 3, 3, 4] * 40,
        "tight gateway (18 +/- 2 ms)": [16, 18, 18, 18, 20] * 40,
        "bursty gateway (8 ms, 5% at 40)": [8] * 190 + [40] * 10,
        "heavy tail (5 ms, 1% at 80)": [5] * 198 + [80] * 2,
    }
    for label, samples in shapes.items():
        informed_comparison(args.floor, samples, label)

    print("\n  Ceilings, independent of L:")
    for F in (20, 10, 5):
        for I in (F, 25, 50, 100):
            print(f"    F={F:<3} I={I:<4} -> at best {I / F:5.2f}x "
                  f"(reached whenever L <= {F} ms)")
        print()


if __name__ == "__main__":
    main()
