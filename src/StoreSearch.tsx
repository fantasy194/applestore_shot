import { useState } from "react";
import { LoaderCircle, MapPin } from "lucide-react";
import type { Bridge } from "./api";
import {
  inventoryCall,
  inventoryError,
  type InventoryStore,
} from "./inventory-api";

export function StoreSearch({
  bridge,
  sku,
  location,
  onLocation,
  onChoose,
  disabled = false,
  chooseDisabled = false,
  multiple = false,
}: {
  bridge: Bridge;
  sku: string;
  location: string;
  onLocation: (location: string) => void;
  onChoose: (stores: InventoryStore[]) => void;
  disabled?: boolean;
  chooseDisabled?: boolean;
  multiple?: boolean;
}) {
  const [stores, setStores] = useState<InventoryStore[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [searched, setSearched] = useState(false);
  return (
    <div className="store-search">
      <div className="store-search-fields">
        <label>
          门店搜索城市 / 邮编
          <input
            value={location}
            disabled={disabled || searching}
            placeholder="例如：上海 / 201101"
            onChange={(event) => {
              onLocation(event.target.value);
              setStores([]);
              setSelected([]);
              setSearched(false);
            }}
          />
        </label>
        <button
          type="button"
          disabled={disabled || searching || !location.trim() || !sku.trim()}
          onClick={async () => {
            setSearching(true);
            setError("");
            setStores([]);
            setSelected([]);
            setSearched(false);
            try {
              const result = await inventoryCall(bridge, "store_search", {
                sku,
                location: location.trim(),
              });
              setStores(Array.isArray(result.stores) ? result.stores : []);
              setSearched(true);
            } catch (error) {
              setError(inventoryError(error));
            } finally {
              setSearching(false);
            }
          }}
        >
          {searching ? (
            <LoaderCircle size={15} className="spin" />
          ) : (
            <MapPin size={15} />
          )}
          从官网读取门店
        </button>
      </div>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {searched && !stores.length && (
        <p role="status" className="muted">
          官网未返回此地区的门店。可尝试门店邮编；这不代表无货。
        </p>
      )}
      {!!stores.length && (
        <div className="store-search-results" aria-label="官网门店结果">
          {stores.map((store) => (
            <label className="store-option" key={store.store_id}>
              <input
                type={multiple ? "checkbox" : "radio"}
                name="discovered-store"
                disabled={disabled || chooseDisabled}
                checked={selected.includes(store.store_id)}
                onChange={(event) => {
                  const next = multiple
                    ? event.target.checked
                      ? [...selected, store.store_id]
                      : selected.filter((id) => id !== store.store_id)
                    : [store.store_id];
                  setSelected(next);
                  if (!multiple) onChoose([store]);
                }}
              />
              <span>
                <strong>{store.store_name}</strong>
                <small>
                  {store.store_id}
                  {store.pickup_message ? ` · ${store.pickup_message}` : ""}
                </small>
              </span>
            </label>
          ))}
          {multiple && (
            <button
              type="button"
              disabled={disabled || chooseDisabled || !selected.length}
              onClick={() => {
                onChoose(
                  selected
                    .map((id) => stores.find((store) => store.store_id === id))
                    .filter((store): store is InventoryStore => !!store),
                );
                setSelected([]);
              }}
            >
              添加选中的 {selected.length} 家门店
            </button>
          )}
        </div>
      )}
    </div>
  );
}
