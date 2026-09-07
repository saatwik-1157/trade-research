import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SizingCalculator } from "./SizingCalculator";

/**
 * The property worth testing here is not the layout. It is that **the browser
 * computes nothing**: every figure on screen arrives from the backend, and a
 * refusal is shown as a refusal rather than as an empty or zeroed quantity.
 */

const calculate = vi.fn();
vi.mock("@/lib/services", () => ({
  sizingService: { calculate: (...args: unknown[]) => calculate(...args) },
}));

const VALID = {
  status: "VALID",
  sizing_mode: "percent_equity",
  symbol: "EURUSD",
  side: "long",
  entry_price: "1.10000",
  stop_loss: "1.09500",
  stop_distance: "0.00500",
  risk_amount: "100.0000",
  risk_per_unit: "500.0000",
  raw_quantity: "0.2000",
  final_quantity: "0.20",
  actual_risk: "100.0000",
  reason: "sized to 0.20 at step 0.01",
  gap: null,
  warnings: [],
  broker_constraints: {
    minimum_volume: "0.01",
    maximum_volume: "100",
    volume_step: "0.01",
    broker_symbol: "EURUSD.R",
  },
  authority: "This is a proposed quantity. It is not an approval and it is not an order.",
};

describe("SizingCalculator", () => {
  beforeEach(() => calculate.mockReset());

  it("displays the backend's figures verbatim and computes none of its own", async () => {
    calculate.mockResolvedValue(VALID);
    render(<SizingCalculator />);
    fireEvent.change(screen.getByLabelText("Entry price"), { target: { value: "1.10000" } });
    fireEvent.change(screen.getByLabelText("Stop loss"), { target: { value: "1.09500" } });
    fireEvent.change(screen.getByLabelText("Account equity"), { target: { value: "10000" } });
    fireEvent.click(screen.getByRole("button", { name: /calculate size/i }));

    await waitFor(() => expect(screen.getByText("VALID")).toBeInTheDocument());
    // The quantity and the stop distance are the backend's strings, not a
    // product of anything this component did.
    expect(screen.getByText("0.20")).toBeInTheDocument();
    expect(screen.getByText("0.00500")).toBeInTheDocument();
    // Target risk and actual risk are both 100.0000 here, and both are shown:
    // a panel that displayed only one could not show the gap when they differ.
    expect(screen.getAllByText("100.0000", { selector: "dd" })).toHaveLength(2);
    expect(screen.getByText(/not an approval/)).toBeInTheDocument();
  });

  it("sends only the fields the chosen mode uses", async () => {
    calculate.mockResolvedValue(VALID);
    render(<SizingCalculator />);
    fireEvent.change(screen.getByLabelText("Entry price"), { target: { value: "1.1" } });
    fireEvent.change(screen.getByLabelText("Account equity"), { target: { value: "10000" } });
    fireEvent.click(screen.getByRole("button", { name: /calculate size/i }));

    await waitFor(() => expect(calculate).toHaveBeenCalled());
    const sent = calculate.mock.calls[0][0];
    expect(sent.sizing_mode).toBe("percent_equity");
    expect(sent.equity).toBe("10000");
    // An untouched field is omitted, never sent as an empty string or a zero:
    // "not stated" and "stated as nothing" are different requests.
    expect(sent).not.toHaveProperty("stop_loss");
    expect(sent).not.toHaveProperty("risk_amount");
    expect(sent).not.toHaveProperty("quantity");
  });

  it("shows a refusal as a refusal, never as a zero quantity", async () => {
    calculate.mockResolvedValue({
      ...VALID,
      status: "REFUSED",
      final_quantity: null,
      actual_risk: null,
      gap: "quantity 0.002 is below the venue minimum 0.01",
    });
    render(<SizingCalculator />);
    fireEvent.click(screen.getByRole("button", { name: /calculate size/i }));

    await waitFor(() => expect(screen.getByText("REFUSED")).toBeInTheDocument());
    expect(screen.getByText(/below the venue minimum/)).toBeInTheDocument();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("reports a failed request instead of showing a stale size", async () => {
    calculate.mockResolvedValueOnce(VALID);
    render(<SizingCalculator />);
    fireEvent.click(screen.getByRole("button", { name: /calculate size/i }));
    await waitFor(() => expect(screen.getByText("VALID")).toBeInTheDocument());

    calculate.mockRejectedValueOnce(new Error("boom"));
    fireEvent.click(screen.getByRole("button", { name: /calculate size/i }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    // The previous answer is cleared. A size left on screen beside a failed
    // request reads as the answer to the request that failed.
    expect(screen.queryByText("VALID")).not.toBeInTheDocument();
  });
});
