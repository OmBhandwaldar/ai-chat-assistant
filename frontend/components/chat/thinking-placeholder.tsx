"use client";

/**
 * Dotted loading pattern shown while a visual is being generated,
 * matching the reference mock's "Editing image..." style placeholder.
 */
export function ThinkingPlaceholder({ label = "Generating..." }: { label?: string }) {
  const cols = 24;
  const rows = 12;
  const dots = Array.from({ length: cols * rows });

  return (
    <div className="relative h-72 w-full overflow-hidden rounded-xl border border-neutral-200 bg-neutral-50">
      <div
        className="grid h-full w-full place-items-center gap-2 p-6"
        style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}
      >
        {dots.map((_, i) => (
          <span
            key={i}
            className="h-1 w-1 rounded-full bg-neutral-300"
            style={{
              // Deterministic "random" so re-renders don't make the dots flicker.
              opacity: 0.3 + (((i * 2654435761) >>> 0) % 100) / 100 * 0.7,
              animationDelay: `${(i % cols) * 40}ms`,
            }}
          />
        ))}
      </div>
      <div className="absolute inset-0 flex items-center justify-center">
        <span className="rounded-full bg-white/90 px-4 py-1.5 text-sm text-neutral-600 shadow-sm">
          {label}
        </span>
      </div>
    </div>
  );
}
