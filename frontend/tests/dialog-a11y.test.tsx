import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Dialog } from "@/components/Dialog";

describe("Dialog accessibility (#656)", () => {
  it("focuses the first field of the body, not the header close button", async () => {
    render(
      <Dialog open onOpenChange={() => undefined} title="Ricollega" closeLabel="Chiudi" footer={<button type="button">Conferma</button>}>
        <button type="button" disabled>Disattivo</button>
        <label>Motivo<input /></label>
      </Dialog>,
    );
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Motivo" })).toHaveFocus());
    expect(screen.getByRole("button", { name: "Chiudi" })).not.toHaveFocus();
  });

  it("without fields focuses the first body control, never a footer action", async () => {
    render(
      <Dialog open onOpenChange={() => undefined} title="Recupera" closeLabel="Chiudi" footer={<button type="button">Conferma</button>}>
        <button type="button">Dettagli</button>
      </Dialog>,
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Dettagli" })).toHaveFocus());
  });

  it("with nothing focusable in the body focuses the dialog itself", async () => {
    render(
      <Dialog open onOpenChange={() => undefined} title="Info" closeLabel="Chiudi" footer={<button type="button">Conferma</button>}>
        <p>Testo</p>
      </Dialog>,
    );
    await waitFor(() => expect(screen.getByRole("dialog")).toHaveFocus());
  });
});
