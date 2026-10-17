import { visualFixtures } from "@/__fixtures__/visuals";
import { VisualRenderer } from "@/components/visuals/registry";

export const metadata = { title: "Visual components — dev" };

export default function DevVisualsPage() {
  return (
    <main className="h-screen overflow-y-auto bg-neutral-50 text-neutral-900">
      <div className="mx-auto flex max-w-3xl flex-col gap-8 px-4 py-10">
        <header>
          <h1 className="text-xl font-semibold">Visual components</h1>
          <p className="mt-1 text-sm text-neutral-500">
            Every visual type rendered from fixtures (no backend needed), including empty and malformed payloads.
          </p>
        </header>
        {visualFixtures.map((f) => (
          <section key={f.name} aria-label={f.name} data-fixture={f.name}>
            <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">{f.name}</h2>
            <VisualRenderer visual={f.visual} />
          </section>
        ))}
      </div>
    </main>
  );
}
