import type { CheckResult } from "./api";
import { evidence } from "./checks";
import { lengthsInText, type LengthUnit } from "./units";

/**
 * One check result in a list: its title and component, what was measured against the limit, and
 * its summary. Lengths (values, limits and those in the summary) follow the mm/in toggle.
 */
export function CheckRow({ result, unit, tone }: { result: CheckResult; unit: LengthUnit; tone?: string }) {
  const values = evidence(result, unit);
  return (
    <li data-check={result.id} className="min-w-0">
      <p className={`truncate ${tone ?? ""}`}>
        {result.title}
        {result.component && ` · ${result.component}`}
      </p>
      {values && (
        <p data-testid="check-values" className="truncate text-[11px] text-[var(--cs-muted)]">
          {values}
        </p>
      )}
      {result.summary && (
        <p data-testid="check-summary" className="text-[11px] text-[var(--cs-faint)]">
          {lengthsInText(result.summary, unit)}
        </p>
      )}
    </li>
  );
}
