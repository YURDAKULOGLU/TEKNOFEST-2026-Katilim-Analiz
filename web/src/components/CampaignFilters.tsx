import type { CampaignFacets, CampaignFilters as FilterValues } from "../api/contracts";

interface CampaignFiltersProps {
  facets: CampaignFacets | undefined;
  filters: FilterValues;
  onChange: (filters: FilterValues) => void;
}

interface FilterSelectProps {
  id: string;
  label: string;
  value: string;
  options: ReadonlyArray<{ value: string; label: string; count?: number }>;
  onChange: (value: string) => void;
}

function FilterSelect({ id, label, value, options, onChange }: FilterSelectProps) {
  return (
    <div className="filter-field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
        <option value="">Tümü</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
            {typeof option.count === "number" ? ` (${option.count})` : ""}
          </option>
        ))}
      </select>
    </div>
  );
}

function withFilter(filters: FilterValues, key: keyof FilterValues, value: string): FilterValues {
  const next = { ...filters };
  if (value) {
    Object.assign(next, { [key]: value });
  } else {
    delete next[key];
  }
  return next;
}

export function CampaignFilters({ facets, filters, onChange }: CampaignFiltersProps) {
  const hasActiveFilters = Object.values(filters).some(Boolean);

  return (
    <form className="filter-panel" aria-label="Kampanya filtreleri" onSubmit={(event) => event.preventDefault()}>
      <div className="filter-panel__heading">
        <div>
          <p className="eyebrow">Bağlamı daraltın</p>
          <h3>Filtreler</h3>
        </div>
        <button
          className="text-button"
          type="button"
          disabled={!hasActiveFilters}
          onClick={() => onChange({})}
        >
          Filtreleri temizle
        </button>
      </div>

      <div className="filter-grid">
        <FilterSelect
          id="filter-bank"
          label="Banka"
          value={filters.bank_id ?? ""}
          options={facets?.banks ?? []}
          onChange={(value) => onChange(withFilter(filters, "bank_id", value))}
        />
        <FilterSelect
          id="filter-family"
          label="Kategori"
          value={filters.product_family ?? ""}
          options={facets?.product_families ?? []}
          onChange={(value) => onChange(withFilter(filters, "product_family", value))}
        />
        <FilterSelect
          id="filter-currency"
          label="Para birimi"
          value={filters.currency ?? ""}
          options={facets?.currencies ?? []}
          onChange={(value) => onChange(withFilter(filters, "currency", value))}
        />
        <FilterSelect
          id="filter-segment"
          label="Müşteri segmenti"
          value={filters.customer_segment ?? ""}
          options={facets?.customer_segments ?? []}
          onChange={(value) => onChange(withFilter(filters, "customer_segment", value))}
        />
        <FilterSelect
          id="filter-channel"
          label="Kanal"
          value={filters.sales_channel ?? ""}
          options={facets?.sales_channels ?? []}
          onChange={(value) => onChange(withFilter(filters, "sales_channel", value))}
        />
        <FilterSelect
          id="filter-validity"
          label="Geçerlilik"
          value={filters.validity ?? ""}
          options={[
            { value: "active", label: "Güncel" },
            { value: "upcoming", label: "Yaklaşan" },
            { value: "expired", label: "Sona ermiş" },
            { value: "unknown", label: "Tarihi belirsiz" },
          ]}
          onChange={(value) => onChange(withFilter(filters, "validity", value))}
        />
      </div>
    </form>
  );
}
