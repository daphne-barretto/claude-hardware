"use client";

import { useShell } from "@/components/sites/claude-shell/AppShell";
import { HardwareWorkspace } from "@/components/sites/claude-shell/HardwareWorkspace";

export default function NewPage() {
  // The first message comes from client state (see AppShell), not the URL.
  const { newCanvas } = useShell();
  return (
    <HardwareWorkspace
      key={newCanvas.id}
      initialPrompt={newCanvas.prompt}
      initialFiles={newCanvas.files}
      initialChoices={newCanvas.choices}
    />
  );
}
