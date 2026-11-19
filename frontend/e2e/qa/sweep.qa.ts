import { census, describeControl } from "./census";
import { focusChecks, stuckLoading, visualChecks } from "./checks";
import { isOwned, ownerOf } from "./known";
import { expect, shot, test, writeFindings, type Finding } from "./live";
import { STATES, VIEWPORTS } from "./states";

// Every QA state at each of its viewports: the visual checks (overlaps, clipping, off-screen,
// sideways scroll, cut-off text, loading that never ends, focus rings, contrast, internal text),
// every control on screen against frontend/e2e/ui-inventory.yaml, and no console errors or failed
// requests on the way. A finding an in-flight branch owns (known.ts) is noted, not failed.

for (const state of STATES)
  for (const vp of state.viewports)
    test(`${state.id}@${vp}: visible properly`, async ({ page, monitor }) => {
      const where = `${state.id}@${vp}`;
      await page.setViewportSize(VIEWPORTS[vp]);
      const findings: Finding[] = [];
      const session = await state.create(page);
      try {
        await state.open(page, session);
      } catch (err) {
        const file = await shot(page, `${where}-unreachable`);
        findings.push({ where, kind: "unreachable", detail: `the state could not be reached: ${String((err as Error).message).split("\n")[0]}`, screenshot: file });
      }
      if (!findings.length) {
        for (const v of await stuckLoading(page)) findings.push({ where, ...v });
        for (const v of await visualChecks(page)) findings.push({ where, ...v });
        const controls = await census(page);
        if (vp === "laptop") {
          for (const c of controls.filter((x) => !x.entry))
            findings.push({ where, kind: "missing-inventory", detail: `${describeControl(c)} is not in frontend/e2e/ui-inventory.yaml` });
        }
        const screenshot = await shot(page, where);
        if (vp !== "phone") for (const v of await focusChecks(page)) findings.push({ where, ...v });
        for (const f of findings) f.screenshot ??= screenshot;
        writeFindings(`sweep-${where}`, [], { controls: controls.map((c) => ({ role: c.role, name: c.name, entry: c.entry, disabled: c.disabled })) });
      }
      for (const p of monitor.problems()) findings.push({ where, kind: p.startsWith("HTTP") ? "http" : "console", detail: p });
      for (const f of findings) f.owner = ownerOf(f);
      writeFindings(`sweep-${where}`, findings, { state: state.id, what: state.what, viewport: vp });
      for (const f of findings.filter(isOwned)) test.info().annotations.push({ type: `owned by ${f.owner}`, description: `${f.kind}: ${f.detail}` });
      // Missing inventory entries are reported, not failed: the inventory has its own test.
      const open = findings.filter((f) => !isOwned(f) && f.kind !== "missing-inventory");
      expect(open.map((f) => `${f.kind}: ${f.detail}`)).toEqual([]);
    });
