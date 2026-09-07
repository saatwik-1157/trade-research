/**
 * The design system. One implementation of each primitive.
 *
 * Import from here, never from the individual files, so a second Button
 * cannot quietly appear beside the first. Existing components (Panel,
 * StatTile, Unavailable, ModeBadge, PageHeader) predate this barrel and are
 * re-exported rather than reimplemented.
 */
export { Badge, type BadgeTone } from "./Badge";
export { Button, type ButtonVariant } from "./Button";
export { ConfirmDialog } from "./ConfirmDialog";
export { EmptyState, ErrorState, LoadingState } from "./States";
export { Field, Input, Select } from "./Input";
export { DataTable, type Column } from "./DataTable";
export { StatusDot, type ServiceState } from "./StatusDot";

export { Panel } from "../Panel";
export { StatTile } from "../StatTile";
export { Unavailable } from "../Unavailable";
export { ModeBadge } from "../ModeBadge";
export { PageHeader } from "../PageHeader";
