import { PageHeader, Panel } from "@/components/ui";
import { DatasetTable } from "@/components/DatasetTable";
import { ModelTable } from "@/components/ModelTable";
import { TrainingJobs } from "@/components/TrainingJobs";
import { ValidationRuns } from "@/components/ValidationRuns";
import { AiDecisions, AiPipeline, AiStrategyConfigs } from "@/components/AiStrategyCenter";
import {
  ModelAlerts,
  ModelHealth,
  MonitoringSnapshots,
} from "@/components/ModelMonitoring";
import {
  ModelDeployments,
  ModelHistory,
  ModelLifecycle,
  ModelVersions,
} from "@/components/ModelRegistry";
import { FeatureRegistry } from "@/components/FeatureRegistry";
import { PlannedSections } from "@/components/PlannedPage";

export default function Page() {
  return (
    <div>
      <PageHeader
        title="AI Lab"
        level={29}
        description="Data, models, training, validation, strategy integration, the model registry and monitoring. A dataset reaches READY only when every leakage check passes; a model refuses rather than defaulting; a finished training run produces a CANDIDATE; and a validation report is a list of named verdicts with no score — a PASS means the candidate may be considered, never that it is approved; a model wired into a strategy can only decline a signal the strategy produced; a version reaches the active state only through paper, never straight from training; and monitoring detects and alerts without ever replacing a model."
      />
      <Panel title="Model health" level={29}>
        <ModelHealth />
      </Panel>
      <Panel title="Monitoring alerts" level={29}>
        <ModelAlerts />
      </Panel>
      <Panel title="Monitoring snapshots" level={29}>
        <MonitoringSnapshots />
      </Panel>
      <Panel title="Model registry" level={28}>
        <ModelVersions />
      </Panel>
      <Panel title="Deployments" level={28}>
        <ModelDeployments />
      </Panel>
      <Panel title="Lifecycle history" level={28}>
        <ModelHistory />
      </Panel>
      <Panel title="The lifecycle" level={28}>
        <ModelLifecycle />
      </Panel>
      <Panel title="AI in the pipeline" level={27}>
        <AiPipeline />
      </Panel>
      <Panel title="AI decisions" level={27}>
        <AiDecisions />
      </Panel>
      <Panel title="Strategy AI configuration" level={27}>
        <AiStrategyConfigs />
      </Panel>
      <Panel title="Validation" level={26}>
        <ValidationRuns />
      </Panel>
      <Panel title="Training" level={25}>
        <TrainingJobs />
      </Panel>
      <Panel title="Models" level={24}>
        <ModelTable />
      </Panel>
      <Panel title="Datasets" level={23}>
        <DatasetTable />
      </Panel>
      <Panel title="Features" level={23}>
        <FeatureRegistry />
      </Panel>
      <PlannedSections
        sections={[
        ]}
      />
    </div>
  );
}
