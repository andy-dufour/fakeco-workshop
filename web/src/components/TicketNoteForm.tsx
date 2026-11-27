import { useState } from "react";
import { api } from "../api";
import { useAction } from "../hooks";
import { ErrorBox } from "./ui";

export function TicketNoteForm({ ticketId, onAdded }: { ticketId: string; onAdded: () => void }) {
  const [text, setText] = useState("");
  const add = useAction(api.addTicketNote);
  return (
    <form
      className="row-nowrap"
      style={{ marginTop: 6 }}
      onSubmit={async (e) => {
        e.preventDefault();
        if (!text.trim()) return;
        if (await add.run(ticketId, text.trim())) {
          setText("");
          onAdded();
        }
      }}
    >
      <input value={text} onChange={(e) => setText(e.target.value)} placeholder={`Add a note to ${ticketId}`} aria-label={`Note for ${ticketId}`} />
      <button className="btn btn-sm" disabled={add.busy || !text.trim()}>Add note</button>
      <ErrorBox error={add.error} />
    </form>
  );
}
