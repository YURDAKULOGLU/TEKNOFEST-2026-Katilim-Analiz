const dateTimeFormatter = new Intl.DateTimeFormat("tr-TR", {
  dateStyle: "medium",
  timeStyle: "short",
});

const dateFormatter = new Intl.DateTimeFormat("tr-TR", {
  dateStyle: "medium",
  timeZone: "UTC",
});

function validDate(value: string): Date | null {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDateTime(value: string): string {
  const parsed = validDate(value);
  return parsed ? dateTimeFormatter.format(parsed) : "Zaman bilgisi yok";
}

export function formatDate(value: string | null): string {
  if (!value) {
    return "Bilinmiyor";
  }
  const parsed = validDate(`${value}T00:00:00Z`);
  return parsed ? dateFormatter.format(parsed) : "Bilinmiyor";
}

export function shortHash(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}
