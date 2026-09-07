import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Unavailable } from "./Unavailable";

describe("Unavailable", () => {
  it("names the level and the reason", () => {
    render(<Unavailable level={19} reason="No OMS." today="python tools/mt5_paper.py --once" />);
    expect(screen.getByRole("note")).toHaveTextContent("L19");
    expect(screen.getByRole("note")).toHaveTextContent("No OMS.");
    expect(screen.getByRole("note")).toHaveTextContent("python tools/mt5_paper.py --once");
  });

  it("renders children inside a disabled fieldset so controls cannot act", () => {
    render(
      <Unavailable level={19} reason="No OMS.">
        <button type="submit">Submit order</button>
      </Unavailable>,
    );
    const button = screen.getByRole("button", { name: "Submit order" });
    expect(button).toBeDisabled();
    expect(button.closest("fieldset")).toHaveAttribute("aria-disabled", "true");
  });
});
