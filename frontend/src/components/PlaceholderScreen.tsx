import { Button } from "@/components/ui/button";

export function PlaceholderScreen() {
  return (
    <main className="min-h-svh bg-canvas px-4 py-10 text-ink">
      <p className="mb-2 text-xs font-extrabold tracking-[0.16em] text-green uppercase">
        Archive
      </p>
      <h1 className="text-4xl tracking-tight">FrostVault</h1>
      <p className="mt-2 text-muted">Frontend toolchain placeholder</p>
      <div className="mt-6">
        <Button type="button">Continue</Button>
      </div>
    </main>
  );
}
