#!/usr/bin/env python3
"""RunPod network-volume multi-attach probe — a one-off, operator-run diagnostic.

QUESTION IT ANSWERS: can the network volume that holds the models (RUNPOD_NETWORK_VOLUME_ID)
be attached to a SECOND pod while the first is still up? RunPod does not document this
either way (plan §10.1). The pod pool is built for both answers — secure+volume first,
community/no-volume fallback on an attach error — but the answer decides what a
scale-up costs: a warm attach (seconds) or a cold model pull (minutes, and container
disk). Run this BEFORE raising BRAIN_POOL_MAX_PODS above 1.

WHAT IT DOES
  1. Lists your pods and confirms one carrying the pool's pod-0 name ("ollama-brain"
     by default) is RUNNING with the volume attached — nothing is created otherwise.
  2. Fetches the cheapest secure GPU in the volume's datacenter under the price ceiling.
  3. Attempts ONE podFindAndDeployOnDemand with the same networkVolumeId, named
     "ollama-brain-probe".
  4. Prints the raw API result (the pod id, or the exact error text), then — unless
     --keep is given — terminates the probe pod immediately so it bills seconds, not
     hours. Without --create it stops after step 2 and only prints what it WOULD do.

It never touches the existing pod, never writes any tenant file, and reads only
RUNPOD_API_KEY, RUNPOD_NETWORK_VOLUME_ID, RUNPOD_DATA_CENTER_ID, RUNPOD_MODEL from
the environment (the same variables the gateway uses).

USAGE
  uv run python scripts/runpod_volume_probe.py            # dry run: list + candidate
  uv run python scripts/runpod_volume_probe.py --create   # actually try the attach
  uv run python scripts/runpod_volume_probe.py --create --keep   # leave the pod up

READING THE RESULT
  "ATTACH OK"        → multi-attach works: pool pods scale warm; keep the defaults.
  "ATTACH REFUSED"   → one pod per volume: pool pods 2..N boot cold via the community
                       fallback. Budget for a ~20 GB pull per scale-up, or give each
                       slot its own volume (future work) before raising max_pods.
  anything else      → an unrelated API error; fix that first, then re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

PROBE_NAME = "ollama-brain-probe"


async def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--create", action="store_true", help="actually attempt the second attach")
    ap.add_argument("--keep", action="store_true", help="do not terminate the probe pod")
    ap.add_argument("--pod-name", default="ollama-brain", help="pool pod 0's name")
    args = ap.parse_args()

    api_key = os.environ.get("RUNPOD_API_KEY", "").strip()
    volume = os.environ.get("RUNPOD_NETWORK_VOLUME_ID", "").strip()
    if not api_key:
        print("RUNPOD_API_KEY is not set", file=sys.stderr)
        return 2
    if not volume:
        print("RUNPOD_NETWORK_VOLUME_ID is not set — nothing to probe (ephemeral disks)")
        return 2

    from brain.runpod_manager import RunPodManager

    # publish_host=False: this manager must never touch settings or the host file.
    m0 = RunPodManager(api_key, pod_name=args.pod_name, publish_host=False)
    pods = await m0._find_existing_pods()
    running = [p for p in pods if p.get("runtime")]
    print(f"pods named {args.pod_name!r}: {len(pods)} (running: {len(running)})")
    if not running:
        print("pod 0 is not running — start it first (log in, or let the reconciler wake it)")
        return 1
    print(f"pod 0: {running[0]['id']}  volume: {volume}")

    probe = RunPodManager(api_key, pod_name=PROBE_NAME, publish_host=False)
    candidates = await probe._create_candidates()
    secure = [c for c in candidates if c["attach_volume"]]
    if not secure:
        print("no SECURE GPU under the price ceiling in the volume's datacenter right now")
        return 1
    gpu = secure[0]["gpu"]
    print(
        f"candidate: {gpu['displayName']} {gpu['memoryInGb']}GB ${gpu['_price']:.2f}/hr "
        f"(SECURE + volume)"
    )
    if not args.create:
        print("dry run — pass --create to attempt the attach")
        return 0

    # Raw create so the exact API error text is visible (the manager logs but hides it).
    import contextlib

    pod_id = None
    try:
        data = await probe._gql(
            """mutation($gpuId: String!, $name: String!, $networkVolumeId: String,
                        $dataCenterId: String) {
            podFindAndDeployOnDemand(input: {
                cloudType: SECURE, gpuCount: 1, volumeInGb: 0, containerDiskInGb: 10,
                minVcpuCount: 2, minMemoryInGb: 15, gpuTypeId: $gpuId, name: $name,
                imageName: "ollama/ollama", ports: "11434/http",
                volumeMountPath: "/root/.ollama", networkVolumeId: $networkVolumeId,
                dataCenterId: $dataCenterId,
                env: [{key: "OLLAMA_HOST", value: "0.0.0.0"}]
            }) { id }
        }""",
            {
                "gpuId": gpu["id"],
                "name": PROBE_NAME,
                "networkVolumeId": volume,
                "dataCenterId": os.environ.get("RUNPOD_DATA_CENTER_ID", "").strip() or None,
            },
        )
        pod_id = data["podFindAndDeployOnDemand"]["id"]
        print(f"ATTACH OK — probe pod {pod_id} created with volume {volume} while pod 0 is up")
    except Exception as e:
        text = str(e)
        verdict = "ATTACH REFUSED" if "volume" in text.lower() else "CREATE FAILED"
        print(f"{verdict} — API said: {text}")
        return 0
    finally:
        if pod_id and not args.keep:
            with contextlib.suppress(Exception):
                await probe._terminate_pod(pod_id)
                print(f"probe pod {pod_id} terminated")
        elif pod_id:
            print(f"probe pod {pod_id} LEFT RUNNING (--keep) — terminate it yourself")
        with contextlib.suppress(Exception):
            if probe._http is not None:
                await probe._http.aclose()
            if m0._http is not None:
                await m0._http.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
