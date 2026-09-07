import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Badge, Button, ConfirmDialog, DataTable, StatusDot } from "./index";
import { EmptyState, ErrorState, LoadingState } from "./States";

describe("Button", () => {
  it("is a button, not a submit, unless asked", () => {
    render(<Button>Go</Button>);
    expect(screen.getByRole("button", { name: "Go" })).toHaveAttribute("type", "button");
  });

  it("respects disabled", () => {
    render(<Button disabled>Go</Button>);
    expect(screen.getByRole("button")).toBeDisabled();
  });
});

describe("StatusDot", () => {
  it("never renders CONNECTED for an unknown service", () => {
    render(<StatusDot state="unknown" />);
    expect(screen.getByText("UNKNOWN")).toBeInTheDocument();
    expect(screen.queryByText("CONNECTED")).not.toBeInTheDocument();
  });

  it("distinguishes degraded from disconnected", () => {
    const { rerender } = render(<StatusDot state="degraded" />);
    expect(screen.getByText("DEGRADED")).toBeInTheDocument();
    rerender(<StatusDot state="disconnected" />);
    expect(screen.getByText("DISCONNECTED")).toBeInTheDocument();
  });
});

describe("DataTable", () => {
  const columns = [
    { key: "a", header: "Alpha", render: (r: { a: string }) => r.a },
    { key: "b", header: "Beta", align: "right" as const },
  ];

  it("shows its header even with no rows, so fields are discoverable", () => {
    render(<DataTable columns={columns} rows={[]} rowKey={() => "x"} />);
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.getByText("no rows")).toBeInTheDocument();
  });

  it("distinguishes loading, error and empty", () => {
    const { rerender } = render(
      <DataTable columns={columns} rows={[]} rowKey={() => "x"} loading />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Loading");

    rerender(<DataTable columns={columns} rows={[]} rowKey={() => "x"} error="broker down" />);
    expect(screen.getByRole("alert")).toHaveTextContent("broker down");

    rerender(<DataTable columns={columns} rows={[]} rowKey={() => "x"} empty="nothing yet" />);
    expect(screen.getByText("nothing yet")).toBeInTheDocument();
  });

  it("renders rows", () => {
    render(
      <DataTable columns={columns} rows={[{ a: "one" }]} rowKey={(r) => r.a} />,
    );
    expect(screen.getByText("one")).toBeInTheDocument();
  });
});

describe("states", () => {
  it("each announces itself to assistive technology", () => {
    const { rerender } = render(<LoadingState what="positions" />);
    expect(screen.getByRole("status")).toHaveTextContent("positions");
    rerender(<ErrorState message="nope" />);
    expect(screen.getByRole("alert")).toHaveTextContent("nope");
    rerender(<EmptyState message="none" hint="not a flat book" />);
    expect(screen.getByText("not a flat book")).toBeInTheDocument();
  });
});

describe("ConfirmDialog", () => {
  it("confirms and cancels through its own buttons", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(
      <ConfirmDialog
        open
        title="Sure?"
        body="body"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onConfirm).toHaveBeenCalled();
  });
});

describe("Badge", () => {
  it("renders its tone and content", () => {
    render(<Badge tone="critical">REJECTED</Badge>);
    expect(screen.getByText("REJECTED")).toBeInTheDocument();
  });
});
