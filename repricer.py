#!/usr/bin/env python3

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
import requests  # type: ignore[import-untyped]
from dotenv import load_dotenv  # type: ignore[import-untyped]

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class SimpleManaPoolPricer:
    """Simple pricer using only ManaPool API pricing sources."""

    def __init__(self):
        self._load_config()
        self._setup_session()

    def _load_config(self):
        """Load configuration from config.json and .env file."""
        config_path = Path(__file__).parent / "config.json"
        if not config_path.exists():
            logger.error("")
            logger.error("=" * 80)
            logger.error("CONFIGURATION ERROR - config.json not found")
            logger.error("=" * 80)
            logger.error("")
            logger.error("Please create a config.json file in the project directory.")
            logger.error("You can copy the example from the repository.")
            logger.error("")
            logger.error("=" * 80)
            sys.exit(1)

        try:
            with open(config_path, "r") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            logger.error("")
            logger.error("=" * 80)
            logger.error("CONFIGURATION ERROR - Invalid config.json")
            logger.error("=" * 80)
            logger.error(f"Error: {e}")
            logger.error("")
            logger.error("Please check that config.json is valid JSON.")
            logger.error("")
            logger.error("=" * 80)
            sys.exit(1)

        self.base_url = os.getenv("API_BASE_URL") or config.get("api", {}).get(
            "base_url", "https://manapool.com/api/v1"
        )
        self.base_url = self.base_url.rstrip("/")
        self.email = os.getenv("API_EMAIL")
        self.access_token = os.getenv("API_TOKEN")

        if not all([self.base_url, self.email, self.access_token]):
            logger.error("")
            logger.error("=" * 80)
            logger.error("CONFIGURATION ERROR - Missing API Credentials")
            logger.error("=" * 80)
            logger.error("")
            logger.error("Please create a .env file with your credentials:")
            logger.error("")
            logger.error("  1. Copy .env.example to .env")
            logger.error("  2. Edit .env and set your credentials:")
            logger.error("")
            logger.error("     API_EMAIL=your-email@example.com")
            logger.error("     API_TOKEN=your-access-token-here")
            logger.error("")
            logger.error("=" * 80)
            sys.exit(1)

        pricing_config = config.get("pricing", {})
        self.dry_run = pricing_config.get("dry_run", True)
        self.pricing_strategy = pricing_config.get("strategy", "lp_plus")
        self.lp_floor_percent = pricing_config.get("lp_floor_percent", 100.0)
        self.min_price = pricing_config.get("min_price", 0.01)
        self.max_reduction_percent = pricing_config.get("max_reduction_percent", 5.0)
        self.price_adjustment_factor = pricing_config.get(
            "price_adjustment_factor", 1.042
        )

        logger.info("=" * 80)
        logger.info("Simple ManaPool Pricer - Configuration")
        logger.info("=" * 80)
        logger.info(f"API Base URL: {self.base_url}")
        logger.info(f"Email: {self.email}")
        logger.info(f"Dry Run: {self.dry_run}")
        logger.info(f"Pricing Strategy: {self.pricing_strategy}")
        logger.info(f"LP+ Floor: {self.lp_floor_percent}%")
        logger.info(f"Min Price: ${self.min_price}")
        logger.info(f"Max Reduction: {self.max_reduction_percent}%")
        logger.info("=" * 80)
        logger.info("")

    def _setup_session(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-ManaPool-Email": self.email,
                "X-ManaPool-Access-Token": self.access_token,
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=3,
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def fetch_inventory(self) -> list[dict[str, Any]]:
        """Fetch all inventory from ManaPool API."""
        logger.info("[1/4] Fetching inventory from ManaPool...")

        all_items = []
        offset = 0
        limit = 10000

        while True:
            url = f"{self.base_url}/seller/inventory"
            params = {"limit": limit, "offset": offset}

            try:
                response: requests.Response = self.session.get(
                    url, params=params, timeout=60
                )
                response.raise_for_status()
                data = response.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to fetch inventory: {e}")
                sys.exit(1)

            inventory = data.get("inventory", [])
            all_items.extend(inventory)

            pagination = data.get("pagination", {})
            total = pagination.get("total", 0)
            returned = pagination.get("returned", 0)

            logger.info(f"  Fetched {len(all_items):,}/{total:,} items")

            if len(all_items) >= total or returned < limit:
                break

            offset += limit

        logger.info(f"  Total inventory items: {len(all_items):,}")
        logger.info("")
        return all_items

    def fetch_prices(self) -> dict[str, dict[str, Any]]:
        """Fetch pricing data from ManaPool API."""
        logger.info("[2/4] Fetching price data from ManaPool...")

        url = f"{self.base_url}/prices/singles"

        try:
            response: requests.Response = self.session.get(url, timeout=60)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch prices: {e}")
            sys.exit(1)

        cards = data.get("data", [])
        logger.info(f"  Received {len(cards):,} price records")

        price_index = {}
        for card in cards:
            scryfall_id = card.get("scryfall_id")
            if scryfall_id:
                price_index[scryfall_id] = card

            product_id = card.get("id") or card.get("product_id")
            if product_id:
                price_index[product_id] = card

        logger.info(
            f"  Indexed {len(price_index):,} unique entries (by scryfall_id and product_id)"
        )
        logger.info("")
        return price_index

    def calculate_new_price(
        self,
        current_price: float,
        nm_price: float | None,
        lp_plus_price: float | None,
        general_price: float | None = None,
    ) -> tuple[float | None, str]:
        if self.pricing_strategy == "nm_only":
            if nm_price is None:
                return None, "No NM price available"
            new_price = nm_price
            reason = f"NM price: ${nm_price:.2f}"

        elif self.pricing_strategy == "lp_plus":
            if lp_plus_price is None:
                return None, "No LP+ price available"
            new_price = lp_plus_price
            reason = f"LP+ price: ${lp_plus_price:.2f}"

        elif self.pricing_strategy == "average":
            prices = [p for p in [nm_price, lp_plus_price] if p is not None]
            if not prices:
                return None, "No pricing data available"
            new_price = sum(prices) / len(prices)
            reason = f"Average of {len(prices)} sources: ${new_price:.2f}"

        elif self.pricing_strategy == "general_low":
            if general_price is None:
                return None, "No general price available"
            new_price = general_price
            reason = f"General/market price: ${general_price:.2f}"

        else:  # nm_with_floor (default)
            if nm_price is None:
                return None, "No NM price available"

            new_price = nm_price
            reason = f"NM: ${nm_price:.2f}"

            if lp_plus_price is not None:
                lp_floor = lp_plus_price * (self.lp_floor_percent / 100.0)
                if new_price < lp_floor:
                    new_price = lp_floor
                    reason += f" (LP+ floor: ${lp_floor:.2f})"

        if new_price < self.min_price:
            new_price = self.min_price
            reason += f" (min: ${self.min_price})"

        if current_price > 0:
            max_reduction = current_price * (self.max_reduction_percent / 100.0)
            min_allowed = current_price - max_reduction
            if new_price < min_allowed:
                new_price = min_allowed
                reason += f" (capped at {self.max_reduction_percent}% reduction)"

        return new_price, reason

    def process_inventory(
        self, inventory: list[dict[str, Any]], price_data: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Process inventory and calculate new prices."""
        logger.info("[3/4] Processing inventory and calculating new prices...")

        updates = []
        stats = {
            "total": 0,
            "no_data": 0,
            "no_change": 0,
            "increased": 0,
            "decreased": 0,
            "errors": 0,
        }

        for item in inventory:
            stats["total"] += 1

            try:
                product = item.get("product", {})
                single = product.get("single", {})

                if not single:
                    stats["no_data"] += 1
                    continue

                scryfall_id = single.get("scryfall_id")
                product_id = product.get("id")

                card_data = None
                if scryfall_id:
                    card_data = price_data.get(scryfall_id)
                if not card_data and product_id:
                    card_data = price_data.get(product_id)

                if not card_data:
                    stats["no_data"] += 1
                    continue

                finish = single.get("finish_id", "NF")
                condition = single.get("condition_id", "NM")
                language = single.get("language_id", "en")
                name = single.get("name", "Unknown")
                set_code = single.get("set", "???")

                current_price = item.get("price_cents", 0) / 100.0

                lookup_id = scryfall_id or product_id
                if not lookup_id:
                    stats["no_data"] += 1
                    continue

                nm_price = self._get_nm_price(card_data, finish)
                lp_plus_price = self._get_lp_plus_price(card_data, finish)
                general_price = self._get_general_price(card_data, finish)

                new_price, reason = self.calculate_new_price(
                    current_price, nm_price, lp_plus_price, general_price
                )

                if new_price is None:
                    stats["no_data"] += 1
                    continue

                new_price = round(new_price, 2)

                if abs(new_price - current_price) < 0.01:
                    stats["no_change"] += 1
                    continue

                if new_price > current_price:
                    stats["increased"] += 1
                elif new_price < current_price:
                    stats["decreased"] += 1

                updates.append(
                    {
                        "scryfall_id": lookup_id,
                        "finish_id": finish,
                        "condition_id": condition,
                        "language_id": language,
                        "price_cents": int(new_price * 100),
                        "quantity": item.get("quantity", 0),
                        "_name": name,
                        "_set": set_code,
                        "_current_price": current_price,
                        "_new_price": new_price,
                        "_reason": reason,
                        "_matched_by": (
                            "scryfall_id"
                            if scryfall_id and price_data.get(scryfall_id)
                            else "product_id"
                        ),
                    }
                )

            except Exception as e:
                stats["errors"] += 1
                logger.debug(f"Error processing {item.get('name', 'unknown')}: {e}")

        logger.info("")
        logger.info("  Processing Summary:")
        logger.info(f"    Total cards: {stats['total']:,}")
        logger.info(f"    No price data: {stats['no_data']:,}")
        logger.info(f"    No change: {stats['no_change']:,}")
        logger.info(f"    Increases: {stats['increased']:,}")
        logger.info(f"    Decreases: {stats['decreased']:,}")
        logger.info(f"    Errors: {stats['errors']:,}")
        logger.info(f"    Total updates: {len(updates):,}")
        logger.info("")

        return updates

    def _get_nm_price(self, card_data: dict, finish: str) -> float | None:
        field_map = {
            "NF": "price_cents_nm",
            "FO": "price_cents_nm_foil",
            "EF": "price_cents_nm_etched",
        }
        field = field_map.get(finish)
        if not field:
            return None

        price_cents = card_data.get(field)
        if price_cents is None:
            return None

        return float(price_cents) / 100.0 / self.price_adjustment_factor

    def _get_lp_plus_price(self, card_data: dict, finish: str) -> float | None:
        field_map = {
            "NF": "price_cents_lp_plus",
            "FO": "price_cents_lp_plus_foil",
            "EF": "price_cents_lp_plus_etched",
        }
        field = field_map.get(finish)
        if not field:
            return None

        price_cents = card_data.get(field)
        if price_cents is None:
            return None

        return float(price_cents) / 100.0 / self.price_adjustment_factor

    def _get_general_price(self, card_data: dict, finish: str) -> float | None:
        if finish == "NF":
            field = "price_cents"
        elif finish == "FO":
            field = "price_cents_foil"
        elif finish == "EF":
            field = "price_cents_etched"
        else:
            return None

        price_cents = card_data.get(field)
        if price_cents is None:
            return None

        return float(price_cents) / 100.0 / self.price_adjustment_factor

    def apply_updates(self, updates: list[dict[str, Any]]) -> bool:
        if not updates:
            logger.info("[4/4] No updates to apply")
            return True

        logger.info(f"[4/4] Reviewing {len(updates):,} price updates...")
        logger.info("")

        logger.info("=" * 80)
        logger.info("PRICE CHANGE PREVIEW")
        logger.info("=" * 80)
        logger.info("")

        self._print_extremes(updates)
        logger.info("")

        self._print_sample_updates(updates)
        logger.info("")

        increases = sum(1 for u in updates if u["_new_price"] > u["_current_price"])
        decreases = sum(1 for u in updates if u["_new_price"] < u["_current_price"])
        total_current = sum(u["_current_price"] for u in updates)
        total_new = sum(u["_new_price"] for u in updates)
        total_change = total_new - total_current

        logger.info("Summary:")
        logger.info(f"  Total updates: {len(updates):,}")
        logger.info(f"  Price increases: {increases:,}")
        logger.info(f"  Price decreases: {decreases:,}")
        logger.info(f"  Total value change: ${total_change:+,.2f}")
        logger.info("")

        if self.dry_run:
            logger.info("=" * 80)
            logger.info("DRY RUN MODE - No changes will be applied")
            logger.info("To apply changes, set dry_run = false in config.json")
            logger.info("=" * 80)
            return True

        logger.info("=" * 80)
        logger.info("READY TO APPLY CHANGES")
        logger.info("=" * 80)
        logger.info("")
        logger.info(
            f"This will update {len(updates):,} prices in your ManaPool inventory."
        )
        logger.info("")

        try:
            response = (
                input("Type 'yes' to confirm and apply these changes: ").strip().lower()
            )
        except (KeyboardInterrupt, EOFError):
            logger.info("\nCancelled by user")
            return False

        if response != "yes":
            logger.info("")
            logger.info("Update cancelled - no changes were made")
            return False

        logger.info("")
        logger.info("Applying updates to ManaPool...")
        logger.info("")

        clean_updates = []
        for update in updates:
            clean_updates.append(
                {
                    "scryfall_id": update["scryfall_id"],
                    "finish_id": update["finish_id"],
                    "condition_id": update["condition_id"],
                    "language_id": update["language_id"],
                    "price_cents": update["price_cents"],
                    "quantity": update["quantity"],
                }
            )

        batch_size = 1500
        total = len(clean_updates)
        num_batches = (total + batch_size - 1) // batch_size

        url = f"{self.base_url}/seller/inventory/scryfall_id"

        for i in range(0, total, batch_size):
            batch_num = i // batch_size + 1
            batch = clean_updates[i : i + batch_size]

            logger.info(f"  Batch {batch_num}/{num_batches}: {len(batch):,} updates...")

            try:
                http_response: requests.Response = self.session.post(
                    url, json=batch, timeout=120
                )
                http_response.raise_for_status()
                logger.info(f"  Batch {batch_num}/{num_batches}: Success!")
            except requests.exceptions.RequestException as e:
                # Try to persist the exact batch payload and some metadata for debugging
                try:
                    resp = getattr(e, "response", None)
                    self._save_failed_batch(batch, batch_num, num_batches, response=resp, error=e)
                except Exception as save_err:
                    logger.error(f"  Failed to save failed batch payload: {save_err}")

                status = None
                text = None
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        status = resp.status_code
                        text = resp.text
                    except Exception:
                        pass

                logger.error(f"  Batch {batch_num}/{num_batches}: Failed - {e}")
                if status:
                    logger.error(f"    HTTP status: {status}")
                if text:
                    # don't flood the logs; trim the response body
                    logger.error(f"    Response body: {text[:1000]}")
                return False

        logger.info("")
        logger.info(f"✓ Successfully updated {total:,} prices!")
        return True

    def _print_extremes(self, updates: list[dict[str, Any]], limit: int = 10):
        updates_with_quantity = [u for u in updates if u.get("quantity", 0) > 0]

        sorted_by_increase = sorted(
            updates_with_quantity,
            key=lambda u: u["_new_price"] - u["_current_price"],
            reverse=True,
        )
        sorted_by_decrease = sorted(
            updates_with_quantity, key=lambda u: u["_new_price"] - u["_current_price"]
        )

        logger.info(f"Top {limit} INCREASES:")
        logger.info(
            f"{'Card':<40} {'Set':<6} {'Current':>10} {'New':>10} {'Change':>12}"
        )
        logger.info("-" * 80)
        for update in sorted_by_increase[:limit]:
            name = update["_name"][:38]
            set_code = update["_set"]
            current = update["_current_price"]
            new = update["_new_price"]
            change = new - current
            change_pct = (change / current * 100) if current > 0 else 0

            logger.info(
                f"{name:<40} {set_code:<6} ${current:>9.2f} ${new:>9.2f} "
                f"+${change:>8.2f} ({change_pct:+.1f}%)"
            )

        logger.info("")
        logger.info(f"Top {limit} DECREASES:")
        logger.info(
            f"{'Card':<40} {'Set':<6} {'Current':>10} {'New':>10} {'Change':>12}"
        )
        logger.info("-" * 80)
        for update in sorted_by_decrease[:limit]:
            name = update["_name"][:38]
            set_code = update["_set"]
            current = update["_current_price"]
            new = update["_new_price"]
            change = new - current
            change_pct = (change / current * 100) if current > 0 else 0

            logger.info(
                f"{name:<40} {set_code:<6} ${current:>9.2f} ${new:>9.2f} "
                f"-${abs(change):>8.2f} ({change_pct:.1f}%)"
            )

    def _print_sample_updates(self, updates: list[dict[str, Any]], limit: int = 20):
        logger.info("Sample price changes (first %d):", min(limit, len(updates)))
        logger.info("")

    def _save_failed_batch(
        self,
        batch: list[dict[str, Any]],
        batch_num: int,
        num_batches: int,
        response: requests.Response | None = None,
        error: Exception | None = None,
        limit: int = 20,
    ) -> None:
        """
        Save the exact batch payload and a small metadata file when a batch fails.
        This creates a 'failed_batches' folder next to the script and writes:
          - failed_batch_{timestamp}_{batch_num}.json  (payload)
          - failed_batch_{timestamp}_{batch_num}.meta  (metadata with status / error)
        """
        try:
            failed_dir = Path(__file__).parent / "failed_batches"
            failed_dir.mkdir(exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            payload_file = failed_dir / f"failed_batch_{timestamp}_b{batch_num}_of_{num_batches}.json"
            meta_file = failed_dir / f"failed_batch_{timestamp}_b{batch_num}_of_{num_batches}.meta"

            # Write payload
            with open(payload_file, "w", encoding="utf-8") as f:
                json.dump(batch, f, indent=2, ensure_ascii=False)

            # Write metadata
            meta = {
                "timestamp": datetime.now().isoformat(),
                "batch_num": batch_num,
                "num_batches": num_batches,
                "payload_file": str(payload_file.name),
                "error": repr(error) if error else None,
            }
            if response is not None:
                try:
                    meta["http_status"] = getattr(response, "status_code", None)
                    meta["response_text_first_200_chars"] = (response.text[:200] if hasattr(response, "text") else None)
                except Exception:
                    # don't let metadata writing fail if response inspection fails
                    pass

            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)

            logger.error(f"  Saved failed batch payload to: {payload_file}")
            logger.error(f"  Saved failure metadata to: {meta_file}")
        except Exception as e:
            logger.error(f"  Error while saving failed batch files: {e}")

        # Also print a small sample of the failed batch to the log for quick inspection
        try:
            logger.info(
                f"{'Scryfall/Product ID':<40} {'Finish':<6} {'Price':>10} {'Qty':>6}"
            )
            logger.info("-" * 70)
            for entry in batch[:limit]:
                sid = entry.get("scryfall_id") or entry.get("product_id") or "-"
                finish = entry.get("finish_id", "-")
                price = entry.get("price_cents", 0) / 100.0
                qty = entry.get("quantity", 0)
                logger.info(f"{sid:<40} {finish:<6} ${price:>9.2f} {qty:>6}")

            if len(batch) > limit:
                logger.info(f"... and {len(batch) - limit:,} more")

            logger.info("")
        except Exception:
            # best-effort logging; don't let this cause further failures
            pass

    def save_report(self, updates: list[dict[str, Any]]):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"price_updates_{timestamp}.json"

        report = {
            "timestamp": datetime.now().isoformat(),
            "dry_run": self.dry_run,
            "strategy": self.pricing_strategy,
            "total_updates": len(updates),
            "updates": updates,
        }

        with open(filename, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Detailed report saved to: {filename}")

    def run(self):
        try:
            inventory = self.fetch_inventory()
            price_data = self.fetch_prices()
            updates = self.process_inventory(inventory, price_data)
            success = self.apply_updates(updates)

            if updates:
                self.save_report(updates)

            logger.info("")
            logger.info("=" * 80)
            if success:
                logger.info("Pricing completed successfully!")
            else:
                logger.info("Pricing completed with errors")
            logger.info("=" * 80)

            return 0 if success else 1

        except KeyboardInterrupt:
            logger.info("\nInterrupted by user")
            return 130
        except Exception as e:
            logger.error(f"\nUnexpected error: {e}")
            import traceback

            traceback.print_exc()
            return 1
        finally:
            if hasattr(self, "session"):
                self.session.close()


def main():
    pricer = SimpleManaPoolPricer()
    sys.exit(pricer.run())


if __name__ == "__main__":
    main()
