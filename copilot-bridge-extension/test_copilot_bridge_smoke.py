#!/usr/bin/env python
"""
Smoke tests for Copilot Bridge v5 — Semantic Search + Workspace Index.

Usage:
    python test_copilot_bridge_smoke.py           # run all tests
    python test_copilot_bridge_smoke.py --no-llm  # skip LLM-dependent tests
"""

import sys
import time
import os

# Add the directory containing copilot_bridge.py to the path if not installed
_script_dir = os.path.dirname(os.path.abspath(__file__))
_dist_dir = os.path.join(os.path.dirname(_script_dir), "copilot-bridge-dist")
if os.path.isdir(_dist_dir):
    sys.path.insert(0, _dist_dir)
from copilot_bridge import CopilotBridge


passed = 0
failed = 0
skipped = 0


def test(name, fn, skip=False):
    """Run a test, print pass/fail/skip."""
    global passed, failed, skipped
    if skip:
        print(f"  SKIP: {name}")
        skipped += 1
        return None
    try:
        result = fn()
        if result:
            print(f"  PASS: {name}")
            passed += 1
            return True
        else:
            print(f"  FAIL: {name} — returned falsy")
            failed += 1
            return False
    except Exception as e:
        print(f"  FAIL: {name} — {e}")
        failed += 1
        return False


def main():
    global passed, failed, skipped
    use_llm = "--no-llm" not in sys.argv

    client = CopilotBridge()

    print("=" * 60)
    print("Copilot Bridge v5 — Smoke Tests")
    print("=" * 60)

    if not client.is_available():
        print("\nERROR: Bridge not running. Start VS Code with the extension.")
        return 1

    health = client.get_health()
    print(f"\nConnected: v{health.get('version', '?')} on port {health.get('port', '?')}")
    print(f"Features: {', '.join(health.get('features', []))}")

    # ============================
    # 1. Connection / Health
    # ============================
    print("\n--- 1. Connection ---")
    test("Health status is ok",
         lambda: health.get("status") == "ok")
    test("Health has version",
         lambda: health.get("version") is not None)
    test("Models available",
         lambda: len(client.get_models()) > 0)
    test("Workspace has root",
         lambda: client.get_workspace().get("root") is not None)

    # ============================
    # 2. Workspace Reindex
    # ============================
    print("\n--- 2. Workspace Reindex ---")
    idx_result = client.reindex()
    test("Reindex completes",
         lambda: idx_result.get("status") in ("ready", "building"))
    test("Reindex returns fileCount > 0",
         lambda: idx_result.get("fileCount", 0) > 0)

    # Wait for index if still building
    for _ in range(10):
        info = client.get_workspace_index()
        if info.get("status") == "ready":
            break
        time.sleep(0.5)

    # ============================
    # 3. Workspace Index Info
    # ============================
    print("\n--- 3. Workspace Index Info ---")
    info = client.get_workspace_index()
    test("Index status is ready",
         lambda: info.get("status") == "ready")
    test("fileCount > 0",
         lambda: info.get("fileCount", 0) > 0)
    test("symbolCount >= 0",
         lambda: info.get("symbolCount", -1) >= 0)
    test("importEdges >= 0",
         lambda: info.get("importEdges", -1) >= 0)
    test("uniqueTerms > 0",
         lambda: info.get("uniqueTerms", 0) > 0)
    test("languages dict present",
         lambda: isinstance(info.get("languages"), dict) and len(info["languages"]) > 0)
    test("buildTimeMs > 0",
         lambda: info.get("buildTimeMs", 0) > 0)

    print(f"  INFO: {info.get('fileCount')} files, {info.get('symbolCount')} symbols, "
          f"{info.get('importEdges')} imports, {info.get('uniqueTerms')} terms, "
          f"built in {info.get('buildTimeMs')}ms")

    # ============================
    # 4. Indexed Files List
    # ============================
    print("\n--- 4. Indexed Files ---")
    files = client.get_workspace_files()
    test("get_workspace_files returns list",
         lambda: isinstance(files, list) and len(files) > 0)

    py_file = next((f["path"] for f in files if f.get("language") == "python"), None)
    ts_file = next((f["path"] for f in files if f.get("language") == "typescript"), None)

    test("Found a Python file in index",
         lambda: py_file is not None)
    test("Found a TypeScript file in index",
         lambda: ts_file is not None)

    if files:
        f = files[0]
        test("File entry has path",
             lambda: "path" in f)
        test("File entry has language",
             lambda: "language" in f)
        test("File entry has size",
             lambda: "size" in f and f["size"] > 0)
        test("File entry has symbols count",
             lambda: "symbols" in f and f["symbols"] >= 0)
        test("File entry has imports count",
             lambda: "imports" in f and f["imports"] >= 0)

    print(f"  INFO: {len(files)} files indexed, py={py_file}, ts={ts_file}")

    # ============================
    # 5. Import Graph
    # ============================
    print("\n--- 5. Import Graph ---")
    full_graph = client.get_import_graph()
    test("Full import graph succeeds",
         lambda: full_graph.get("success"))
    test("Full graph has edges",
         lambda: isinstance(full_graph.get("edges"), list))
    test("Full graph has fileCount",
         lambda: full_graph.get("fileCount", 0) > 0)

    print(f"  INFO: {len(full_graph.get('edges', []))} edges across {full_graph.get('fileCount')} files")

    if py_file:
        fg = client.get_import_graph(py_file)
        test(f"Import graph for {py_file} succeeds",
             lambda: fg.get("success"))
        test("File graph has imports list",
             lambda: isinstance(fg.get("imports"), list))
        test("File graph has importedBy list",
             lambda: isinstance(fg.get("importedBy"), list))
        test("File graph has symbols list",
             lambda: isinstance(fg.get("symbols"), list))
        print(f"  INFO: {py_file}: {len(fg.get('imports', []))} imports, "
              f"{len(fg.get('importedBy', []))} importedBy, {len(fg.get('symbols', []))} symbols")

    # ============================
    # 6. Related Files
    # ============================
    print("\n--- 6. Related Files ---")
    if py_file:
        rel = client.get_related_files(py_file)
        test(f"Related files for {py_file} succeeds",
             lambda: rel.get("success"))
        test("Related has file field",
             lambda: rel.get("file") == py_file)
        test("Related results is list",
             lambda: isinstance(rel.get("related"), list))
        related_list = rel.get("related", [])
        if related_list:
            r0 = related_list[0]
            test("Related entry has path",
                 lambda: "path" in r0)
            test("Related entry has score",
                 lambda: "score" in r0 and r0["score"] > 0)
            test("Related entry has reason",
                 lambda: "reason" in r0 and len(r0["reason"]) > 0)
        print(f"  INFO: {len(related_list)} files related to {py_file}")

    # ============================
    # 7. TF-IDF Search (no LLM)
    # ============================
    print("\n--- 7. TF-IDF Search (no LLM) ---")
    r1 = client.semantic_search("import", max_results=10, use_llm=False)
    test("TF-IDF search 'import' succeeds",
         lambda: r1.get("success"))
    test("TF-IDF returns results",
         lambda: len(r1.get("results", [])) > 0)
    test("TF-IDF meta has indexedFiles",
         lambda: r1.get("meta", {}).get("indexedFiles", 0) > 0)
    test("TF-IDF meta llmRanked is false",
         lambda: r1.get("meta", {}).get("llmRanked") is False)

    if r1.get("results"):
        r0 = r1["results"][0]
        test("Result has path",
             lambda: "path" in r0)
        test("Result has score > 0",
             lambda: r0.get("score", 0) > 0)
        test("Result has language",
             lambda: "language" in r0)
        test("Result has summary",
             lambda: "summary" in r0 and len(r0["summary"]) > 0)
        print(f"  INFO: Top result: {r0['path']} (score: {r0['score']})")

    r2 = client.semantic_search("class", max_results=5, use_llm=False)
    test("TF-IDF search 'class' returns results",
         lambda: len(r2.get("results", [])) > 0)

    r3 = client.semantic_search("CopilotBridge", max_results=10, use_llm=False)
    test("TF-IDF search 'CopilotBridge' returns results",
         lambda: len(r3.get("results", [])) > 0)
    # The copilot_bridge.py file should rank high for this query
    if r3.get("results"):
        paths = [r["path"] for r in r3["results"]]
        test("copilot_bridge file in results for 'CopilotBridge'",
             lambda: any("copilot_bridge" in p.lower().replace("\\", "/") for p in paths))

    r4 = client.semantic_search("xyznonexistentterm12345", max_results=5, use_llm=False)
    test("TF-IDF for nonsense returns few/no results",
         lambda: len(r4.get("results", [])) <= 3)

    # ============================
    # 8. Semantic Search (with LLM)
    # ============================
    print("\n--- 8. Semantic Search (with LLM) ---")
    r5 = client.semantic_search(
        "find the main entry point of the application",
        max_results=10, use_llm=True
    )
    test("LLM semantic search succeeds",
         lambda: r5.get("success"), skip=not use_llm)
    test("LLM semantic search returns results",
         lambda: len(r5.get("results", [])) > 0, skip=not use_llm)
    meta5 = r5.get("meta", {})
    test("LLM expansion terms generated",
         lambda: meta5.get("expansionTerms") is not None and len(meta5.get("expansionTerms", [])) > 0,
         skip=not use_llm)
    test("LLM ranking applied",
         lambda: meta5.get("llmRanked") is True, skip=not use_llm)

    if use_llm and r5.get("results"):
        print(f"  INFO: Expansion terms: {meta5.get('expansionTerms')}")
        print(f"  INFO: Top LLM result: {r5['results'][0]['path']} "
              f"(score: {r5['results'][0]['score']}, reason: {r5['results'][0].get('reason', 'n/a')})")

    r6 = client.semantic_search(
        "how does the git integration work",
        max_results=5, use_llm=True
    )
    test("LLM search 'git integration' succeeds",
         lambda: r6.get("success"), skip=not use_llm)
    if use_llm and r6.get("results"):
        paths6 = [r["path"] for r in r6["results"]]
        test("Git-related files in results",
             lambda: any("git" in p.lower() or "extension" in p.lower() or "bridge" in p.lower() for p in paths6),
             skip=not use_llm)

    # ============================
    # 9. Edge Cases
    # ============================
    print("\n--- 9. Edge Cases ---")
    test("Semantic search with empty query",
         lambda: client.semantic_search("", use_llm=False).get("success") is not False)
    test("Related files for non-existent file",
         lambda: client.get_related_files("nonexistent.py").get("success") is False)
    test("Import graph for non-existent file",
         lambda: client.get_import_graph("nonexistent.py").get("success") is False)

    # ============================
    # Summary
    # ============================
    total = passed + failed + skipped
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {passed} passed, {failed} failed, {skipped} skipped ({total} total)")
    print(f"{'=' * 60}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
