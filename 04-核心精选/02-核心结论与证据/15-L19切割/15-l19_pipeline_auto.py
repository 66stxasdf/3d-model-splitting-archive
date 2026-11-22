#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L19 统一入口：一份闭环割线 JSON 自动路由到平面 / 曲面分支并真正执行。

  closed=false                  -> REJECTED（不切，已有产物不动）
  closed=true, 共面 RMS<0.5 mm  -> l19_pipeline.py        （半空间平面布尔，11 项复验）
  closed=true, 共面 RMS>=0.5mm  -> l19_pipeline_curved.py （Delaunay 膜曲面布尔，10 项复验）
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
THRESH = 0.5


def plane_rms(pts):
    P = np.asarray(pts, dtype=np.float64)
    c = P.mean(0)
    _, _, vh = np.linalg.svd(P - c, full_matrices=False)
    n = vh[-1]
    return float(np.sqrt((((P - c) @ n) ** 2).mean()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seam", required=True)
    ap.add_argument("--tag", default="auto")
    a = ap.parse_args()
    d = json.load(open(a.seam, encoding="utf-8"))
    pts = (d.get("loop_points") or d.get("strokes", [{}])[0].get("pts")
           or [p for sg in d.get("segments", []) for p in sg.get("pts", [])]
           or [x.get("point") for x in d.get("anchors", []) if x.get("point")])
    rep = dict(seam=a.seam, tag=a.tag, n_points=len(pts),
               closed=bool(d.get("closed")), threshold_mm=THRESH)
    if not d.get("closed"):
        rep.update(route="REJECTED", reason="割线未闭合，后台无法定义切割体内外")
        json.dump(rep, open(os.path.join(HERE, f"l19_auto_{a.tag}.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=2)
        print("ROUTE REJECTED", flush=True)
        return 0
    rms = plane_rms(pts)
    rep["coplanar_rms_mm"] = rms
    branch = "plane" if rms < THRESH else "curved"
    script = "l19_pipeline.py" if branch == "plane" else "l19_pipeline_curved.py"
    rep["route"] = branch
    rep["script"] = script
    t = time.time()
    r = subprocess.run([sys.executable, script, "--seam", a.seam,
                        "--tag", "auto_" + branch],
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1200)
    rep["child_returncode"] = r.returncode
    rep["elapsed_s"] = round(time.time() - t, 2)
    rep["child_stdout_tail"] = (r.stdout or "")[-800:]
    out = os.path.join(HERE, f"l19_pipeline_auto_{branch}.json")
    if os.path.isfile(out):
        child = json.load(open(out, encoding="utf-8"))
        rep["child_status"] = child.get("status")
        rep["child_verify_all_ok"] = child.get("verify_all_ok")
        rep["child_checks_passed"] = (sum(child["verify_checks"].values())
                                      if child.get("verify_checks") else None)
        rep["child_checks_total"] = (len(child["verify_checks"])
                                     if child.get("verify_checks") else None)
    rep["status"] = ("SUCCESS" if r.returncode == 0
                     and rep.get("child_status") == "SUCCESS" else "FAILED")
    json.dump(rep, open(os.path.join(HERE, f"l19_auto_{a.tag}.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    print("ROUTE", branch, "rms", round(rms, 4), "->", script,
          "status", rep["status"], rep["elapsed_s"], "s", flush=True)
    return 0 if rep["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
