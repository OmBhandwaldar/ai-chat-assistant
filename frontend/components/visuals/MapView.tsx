"use client";

// Leaflet touches `window` at import time, so this module must only ever be loaded
// client-side — the registry imports it via next/dynamic({ ssr: false }).
// Pins are SVG circle markers, so no marker image assets are needed (bundlers break those URLs).
import { useMemo } from "react";
import { latLngBounds } from "leaflet";
import { CircleMarker, MapContainer, Popup, TileLayer } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import { normalizePins } from "@/lib/visual-data";
import { NoData, VisualCard } from "./shared";

export default function MapView({ data }: { data?: unknown }) {
  const pins = useMemo(() => normalizePins(data), [data]);
  const bounds = useMemo(
    () => (pins.length > 0 ? latLngBounds(pins.map((p) => [p.lat, p.lon] as [number, number])) : null),
    [pins]
  );
  if (!bounds) return <NoData />;

  return (
    <VisualCard>
      {/* `isolate` keeps Leaflet's high z-index panes beneath app dropdowns/dialogs. */}
      <div className="isolate h-80 overflow-hidden rounded-lg">
        <MapContainer
          bounds={bounds.pad(0.2)}
          maxZoom={16}
          scrollWheelZoom={false}
          className="h-full w-full"
        >
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          {pins.map((p, i) => (
            <CircleMarker
              key={`${p.asset_id ?? i}-${p.date ?? i}`}
              center={[p.lat, p.lon]}
              radius={8}
              pathOptions={{ color: "#ffffff", weight: 2, fillColor: "#dc2626", fillOpacity: 0.9 }}
            >
              <Popup>
                <div className="text-xs text-neutral-900">
                  <div className="font-semibold">{p.name ?? p.asset_id ?? "Spill"}</div>
                  {p.asset_id && p.name && <div>{p.asset_id}</div>}
                  {p.date && <div>{p.date}</div>}
                  {p.bbl !== undefined && <div>{p.bbl} bbl</div>}
                </div>
              </Popup>
            </CircleMarker>
          ))}
        </MapContainer>
      </div>
      <p className="mt-2 text-xs text-neutral-500">
        {pins.length} {pins.length === 1 ? "pin" : "pins"}
      </p>
    </VisualCard>
  );
}
