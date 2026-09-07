import { levelTag } from "@/lib/nav";

export function PageHeader({
  title,
  level,
  description,
}: {
  title: string;
  level: number;
  description: string;
}) {
  return (
    <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        <p className="text-sm text-muted">{description}</p>
      </div>
      <span className="font-mono text-body text-muted">{levelTag(level)}</span>
    </div>
  );
}
