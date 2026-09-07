import { PageHeader } from "./PageHeader";
import { Panel } from "./Panel";
import { Unavailable } from "./Unavailable";

export interface PlannedSection {
  title: string;
  level: number;
  reason: string;
  today?: string;
}

/**
 * A page whose backend does not exist yet. Every section names its level and,
 * where the research toolkit already does the job on the command line, the
 * tool that does it, so the page is a map of real work rather than a mock.
 */
export function PlannedPage({
  title,
  level,
  description,
  sections,
}: {
  title: string;
  level: number;
  description: string;
  sections: PlannedSection[];
}) {
  return (
    <div>
      <PageHeader title={title} level={level} description={description} />
      <PlannedSections sections={sections} />
    </div>
  );
}

/**
 * The section grid on its own, for a page that is partly real.
 *
 * Extracted at L23 rather than copied: `/ai-lab` now has working panels above
 * its planned ones, and a second grid rendering the same "not built" contract
 * would be a second place for that wording to drift.
 */
export function PlannedSections({ sections }: { sections: PlannedSection[] }) {
  return (
    <div className="grid gap-4 md:grid-cols-2">
      {sections.map((s) => (
        <Panel key={s.title} title={s.title} level={s.level}>
          <Unavailable level={s.level} reason={s.reason} today={s.today} />
        </Panel>
      ))}
    </div>
  );
}
