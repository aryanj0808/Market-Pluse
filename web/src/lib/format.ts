const nf2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const compactFmt = new Intl.NumberFormat("en-IN", { notation: "compact", maximumFractionDigits: 1 });

export const px = (x: number | null | undefined) => (x === null || x === undefined ? "–" : nf2.format(x));
export const px0 = (x: number | null | undefined) => (x === null || x === undefined ? "–" : nf0.format(x));
export const compact = (x: number | null | undefined) => (x === null || x === undefined ? "–" : compactFmt.format(x));
export const pct = (x: number | null | undefined, digits = 2) =>
  x === null || x === undefined ? "–" : `${x >= 0 ? "+" : ""}${x.toFixed(digits)}%`;
export const signed = (x: number, digits = 2) => `${x >= 0 ? "+" : ""}${x.toFixed(digits)}`;

export const istTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit", hour12: false });
export const istDateTime = (iso: string) =>
  new Date(iso).toLocaleString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

export const upDown = (x: number) => (x > 0 ? "text-emerald-400" : x < 0 ? "text-rose-400" : "text-slate-400");
