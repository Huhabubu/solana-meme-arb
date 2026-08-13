use std::collections::{HashMap, HashSet};

use anyhow::{bail, Context, Result};

use crate::model::PoolInfo;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DependencyKind {
    PoolState,
    TokenVault,
    TickArray,
    Oracle,
    BinArray,
    BitmapExtension,
    TokenMint,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DependencyAccount {
    pub address: String,
    pub kind: DependencyKind,
}

impl DependencyAccount {
    pub fn new(address: impl Into<String>, kind: DependencyKind) -> Result<Self> {
        let address = address.into();
        if address.trim().is_empty() {
            bail!("dependency account address cannot be empty");
        }
        Ok(Self { address, kind })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PoolDependencies {
    pub pool: PoolInfo,
    pub accounts: Vec<DependencyAccount>,
}

impl PoolDependencies {
    pub fn new(pool: PoolInfo, accounts: Vec<DependencyAccount>) -> Result<Self> {
        if accounts.is_empty() {
            bail!("pool dependency list cannot be empty: {}", pool.address);
        }

        let mut kinds_by_address = HashMap::new();
        let mut deduped = Vec::with_capacity(accounts.len());
        for account in accounts {
            match kinds_by_address.get(&account.address) {
                Some(existing_kind) if *existing_kind != account.kind => {
                    bail!(
                        "dependency account {} has conflicting kinds {:?} and {:?}",
                        account.address,
                        existing_kind,
                        account.kind
                    );
                }
                Some(_) => continue,
                None => {
                    kinds_by_address.insert(account.address.clone(), account.kind);
                    deduped.push(account);
                }
            }
        }

        Ok(Self {
            pool,
            accounts: deduped,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VersionedAccountData {
    pub slot: u64,
    pub owner: String,
    pub data: Vec<u8>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApplyUpdateResult {
    pub accepted: bool,
    pub affected_pools: Vec<String>,
}

#[derive(Debug, Default)]
pub struct QuoteState {
    dependencies_by_pool: HashMap<String, PoolDependencies>,
    pools_by_account: HashMap<String, HashSet<String>>,
    account_data: HashMap<String, VersionedAccountData>,
}

impl QuoteState {
    pub fn new() -> Self {
        Self::default()
    }

    /// 注册或替换一个池的完整依赖集合。替换时先清理旧反向索引，避免旧 TickArray / BinArray
    /// 在价格跨区间后继续错误触发该池。
    pub fn replace_pool_dependencies(&mut self, dependencies: PoolDependencies) -> Result<()> {
        let pool_address = dependencies.pool.address.clone();
        if pool_address.trim().is_empty() {
            bail!("pool address cannot be empty");
        }

        if let Some(previous) = self.dependencies_by_pool.remove(&pool_address) {
            self.remove_reverse_index(&previous);
        }

        for account in &dependencies.accounts {
            self.pools_by_account
                .entry(account.address.clone())
                .or_default()
                .insert(pool_address.clone());
        }
        self.dependencies_by_pool.insert(pool_address, dependencies);
        Ok(())
    }

    pub fn dependencies_for_pool(&self, pool_address: &str) -> Option<&PoolDependencies> {
        self.dependencies_by_pool.get(pool_address)
    }

    pub fn affected_pools(&self, account_address: &str) -> Vec<String> {
        let mut pools = self
            .pools_by_account
            .get(account_address)
            .map(|pools| pools.iter().cloned().collect::<Vec<_>>())
            .unwrap_or_default();
        pools.sort();
        pools
    }

    pub fn dependency_kind(
        &self,
        pool_address: &str,
        account_address: &str,
    ) -> Option<DependencyKind> {
        self.dependencies_by_pool
            .get(pool_address)?
            .accounts
            .iter()
            .find(|account| account.address == account_address)
            .map(|account| account.kind)
    }

    pub fn unique_dependency_addresses(&self) -> Vec<String> {
        let mut addresses = self.pools_by_account.keys().cloned().collect::<Vec<_>>();
        addresses.sort();
        addresses
    }

    /// 同 slot 的新通知仍接受，因为一个账户在同一 slot 内可能出现新的最终状态；
    /// 只有严格更旧的 slot 才忽略，防止 RPC/WSS 乱序让本地状态倒退。
    pub fn apply_account_update(
        &mut self,
        address: &str,
        update: VersionedAccountData,
    ) -> Result<ApplyUpdateResult> {
        if !self.pools_by_account.contains_key(address) {
            bail!("account update is not registered as a quote dependency: {address}");
        }
        if update.owner.trim().is_empty() {
            bail!("account update owner cannot be empty");
        }

        if self
            .account_data
            .get(address)
            .is_some_and(|current| update.slot < current.slot)
        {
            return Ok(ApplyUpdateResult {
                accepted: false,
                affected_pools: self.affected_pools(address),
            });
        }

        self.account_data.insert(address.to_owned(), update);
        Ok(ApplyUpdateResult {
            accepted: true,
            affected_pools: self.affected_pools(address),
        })
    }

    pub fn missing_accounts_for_pool(&self, pool_address: &str) -> Result<Vec<String>> {
        let dependencies = self
            .dependencies_by_pool
            .get(pool_address)
            .with_context(|| format!("unknown pool dependencies: {pool_address}"))?;
        Ok(dependencies
            .accounts
            .iter()
            .filter(|account| !self.account_data.contains_key(&account.address))
            .map(|account| account.address.clone())
            .collect())
    }

    fn remove_reverse_index(&mut self, dependencies: &PoolDependencies) {
        let pool_address = &dependencies.pool.address;
        for account in &dependencies.accounts {
            let remove_account_key =
                if let Some(pools) = self.pools_by_account.get_mut(&account.address) {
                    pools.remove(pool_address);
                    pools.is_empty()
                } else {
                    false
                };
            if remove_account_key {
                self.pools_by_account.remove(&account.address);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Dex;

    fn pool(address: &str) -> PoolInfo {
        PoolInfo {
            dex: Dex::Orca,
            address: address.into(),
            pool_type: "test".into(),
            program_id: Some("program".into()),
            mint_a: "A".into(),
            mint_b: "B".into(),
            tvl_usd: 1_000.0,
        }
    }

    fn dep(address: &str, kind: DependencyKind) -> DependencyAccount {
        DependencyAccount::new(address, kind).unwrap()
    }

    fn version(slot: u64, byte: u8) -> VersionedAccountData {
        VersionedAccountData {
            slot,
            owner: "owner".into(),
            data: vec![byte],
        }
    }

    #[test]
    fn dependency_account_rejects_empty_address() {
        assert!(DependencyAccount::new(" ", DependencyKind::PoolState).is_err());
    }

    #[test]
    fn pool_dependencies_deduplicate_exact_duplicate_and_reject_kind_conflict() {
        let dependencies = PoolDependencies::new(
            pool("pool-a"),
            vec![
                dep("pool-a", DependencyKind::PoolState),
                dep("vault-a", DependencyKind::TokenVault),
                dep("vault-a", DependencyKind::TokenVault),
            ],
        )
        .unwrap();
        assert_eq!(dependencies.accounts.len(), 2);

        assert!(PoolDependencies::new(
            pool("pool-a"),
            vec![
                dep("same", DependencyKind::TokenVault),
                dep("same", DependencyKind::TickArray),
            ],
        )
        .is_err());
    }

    #[test]
    fn register_builds_reverse_index_and_shared_account_maps_to_multiple_pools() {
        let mut state = QuoteState::new();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-a"),
                    vec![
                        dep("pool-a", DependencyKind::PoolState),
                        dep("shared", DependencyKind::TokenMint),
                    ],
                )
                .unwrap(),
            )
            .unwrap();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-b"),
                    vec![
                        dep("pool-b", DependencyKind::PoolState),
                        dep("shared", DependencyKind::TokenMint),
                    ],
                )
                .unwrap(),
            )
            .unwrap();

        assert_eq!(state.affected_pools("shared"), vec!["pool-a", "pool-b"]);
        assert_eq!(state.unique_dependency_addresses().len(), 3);
    }

    #[test]
    fn replacing_dependencies_removes_stale_dynamic_reverse_index() {
        let mut state = QuoteState::new();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-a"),
                    vec![
                        dep("pool-a", DependencyKind::PoolState),
                        dep("tick-old", DependencyKind::TickArray),
                    ],
                )
                .unwrap(),
            )
            .unwrap();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-a"),
                    vec![
                        dep("pool-a", DependencyKind::PoolState),
                        dep("tick-new", DependencyKind::TickArray),
                    ],
                )
                .unwrap(),
            )
            .unwrap();

        assert!(state.affected_pools("tick-old").is_empty());
        assert_eq!(state.affected_pools("tick-new"), vec!["pool-a"]);
        assert_eq!(
            state.dependency_kind("pool-a", "tick-new"),
            Some(DependencyKind::TickArray)
        );
    }

    #[test]
    fn newer_and_same_slot_updates_are_accepted_but_older_update_is_ignored() {
        let mut state = QuoteState::new();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-a"),
                    vec![dep("vault", DependencyKind::TokenVault)],
                )
                .unwrap(),
            )
            .unwrap();

        assert!(
            state
                .apply_account_update("vault", version(10, 1))
                .unwrap()
                .accepted
        );
        assert!(
            state
                .apply_account_update("vault", version(10, 2))
                .unwrap()
                .accepted
        );
        assert_eq!(state.account_data.get("vault").unwrap().data, vec![2]);

        let result = state.apply_account_update("vault", version(9, 3)).unwrap();
        assert!(!result.accepted);
        assert_eq!(result.affected_pools, vec!["pool-a"]);
        assert_eq!(state.account_data.get("vault").unwrap().data, vec![2]);
    }

    #[test]
    fn apply_update_rejects_unknown_account_and_empty_owner() {
        let mut state = QuoteState::new();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-a"),
                    vec![dep("vault", DependencyKind::TokenVault)],
                )
                .unwrap(),
            )
            .unwrap();

        assert!(state
            .apply_account_update("unknown", version(1, 1))
            .is_err());
        assert!(state
            .apply_account_update(
                "vault",
                VersionedAccountData {
                    slot: 1,
                    owner: "".into(),
                    data: vec![]
                }
            )
            .is_err());
    }

    #[test]
    fn missing_accounts_tracks_initial_snapshot_completeness() {
        let mut state = QuoteState::new();
        state
            .replace_pool_dependencies(
                PoolDependencies::new(
                    pool("pool-a"),
                    vec![
                        dep("pool-a", DependencyKind::PoolState),
                        dep("vault", DependencyKind::TokenVault),
                    ],
                )
                .unwrap(),
            )
            .unwrap();

        assert_eq!(state.missing_accounts_for_pool("pool-a").unwrap().len(), 2);
        state.apply_account_update("pool-a", version(1, 1)).unwrap();
        assert_eq!(
            state.missing_accounts_for_pool("pool-a").unwrap(),
            vec!["vault"]
        );
        state.apply_account_update("vault", version(1, 2)).unwrap();
        assert!(state
            .missing_accounts_for_pool("pool-a")
            .unwrap()
            .is_empty());
        assert!(state.missing_accounts_for_pool("unknown").is_err());
    }
}
