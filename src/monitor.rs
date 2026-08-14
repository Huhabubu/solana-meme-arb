use std::{
    collections::{hash_map::Entry, HashMap},
    time::Duration,
};

use anyhow::{bail, Context, Result};

use crate::{helius::RawAccountUpdate, state::DependencyKind};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpportunityMonitorConfig {
    pub target_updates: Option<usize>,
    pub max_duration: Option<Duration>,
    pub update_timeout: Duration,
    pub max_reconnects: Option<usize>,
}

impl OpportunityMonitorConfig {
    pub fn parse(
        target_updates: Option<&str>,
        max_seconds: Option<&str>,
        update_timeout_seconds: Option<&str>,
        max_reconnects: Option<&str>,
    ) -> Result<Self> {
        let target_updates =
            parse_optional_positive_usize("OPPORTUNITY_MONITOR_UPDATES", target_updates)?;
        let max_duration =
            parse_optional_positive_u64("OPPORTUNITY_MONITOR_MAX_SECONDS", max_seconds)?
                .map(Duration::from_secs);
        let update_timeout = Duration::from_secs(
            parse_optional_positive_u64(
                "OPPORTUNITY_MONITOR_UPDATE_TIMEOUT_SECONDS",
                update_timeout_seconds,
            )?
            .unwrap_or(45),
        );
        let max_reconnects =
            parse_optional_nonnegative_usize("OPPORTUNITY_MONITOR_MAX_RECONNECTS", max_reconnects)?;
        Ok(Self {
            target_updates,
            max_duration,
            update_timeout,
            max_reconnects,
        })
    }

    pub fn target_reached(&self, processed_updates: usize) -> bool {
        self.target_updates
            .is_some_and(|target| processed_updates >= target)
    }

    pub fn wait_timeout(&self, elapsed: Duration) -> Option<Duration> {
        match self.max_duration {
            Some(max_duration) => {
                let remaining = max_duration.checked_sub(elapsed)?;
                if remaining.is_zero() {
                    None
                } else {
                    Some(self.update_timeout.min(remaining))
                }
            }
            None => Some(self.update_timeout),
        }
    }

    pub fn reconnect_allowed(&self, reconnects_used: usize) -> bool {
        self.max_reconnects
            .is_none_or(|limit| reconnects_used < limit)
    }
}

fn parse_optional_positive_usize(name: &str, value: Option<&str>) -> Result<Option<usize>> {
    value
        .map(|value| {
            let parsed = value
                .parse::<usize>()
                .with_context(|| format!("{name} must be a positive integer"))?;
            if parsed == 0 {
                bail!("{name} must be greater than zero");
            }
            Ok(parsed)
        })
        .transpose()
}

fn parse_optional_nonnegative_usize(name: &str, value: Option<&str>) -> Result<Option<usize>> {
    value
        .map(|value| {
            value
                .parse::<usize>()
                .with_context(|| format!("{name} must be a non-negative integer"))
        })
        .transpose()
}

fn parse_optional_positive_u64(name: &str, value: Option<&str>) -> Result<Option<u64>> {
    value
        .map(|value| {
            let parsed = value
                .parse::<u64>()
                .with_context(|| format!("{name} must be a positive integer"))?;
            if parsed == 0 {
                bail!("{name} must be greater than zero");
            }
            Ok(parsed)
        })
        .transpose()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UpdateNovelty {
    New,
    Duplicate,
    Stale,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SeenUpdate {
    slot: u64,
    owner: String,
    data: Vec<u8>,
}

#[derive(Debug, Default)]
pub struct UpdateWatermark {
    latest: HashMap<String, SeenUpdate>,
}

impl UpdateWatermark {
    pub fn classify(&mut self, update: &RawAccountUpdate) -> UpdateNovelty {
        match self.latest.entry(update.address.clone()) {
            Entry::Vacant(entry) => {
                entry.insert(seen_update(update));
                UpdateNovelty::New
            }
            Entry::Occupied(mut entry) => {
                let previous = entry.get();
                if update.slot < previous.slot {
                    return UpdateNovelty::Stale;
                }
                if update.slot == previous.slot
                    && update.owner == previous.owner
                    && update.data == previous.data
                {
                    return UpdateNovelty::Duplicate;
                }
                entry.insert(seen_update(update));
                UpdateNovelty::New
            }
        }
    }
}

fn seen_update(update: &RawAccountUpdate) -> SeenUpdate {
    SeenUpdate {
        slot: update.slot,
        owner: update.owner.clone(),
        data: update.data.clone(),
    }
}

pub fn subscription_sets_equal(left: &[String], right: &[String]) -> bool {
    left.len() == right.len() && left.iter().all(|address| right.contains(address))
}

pub fn dependency_update_may_change_set(kind: DependencyKind) -> bool {
    matches!(
        kind,
        DependencyKind::PoolState | DependencyKind::BitmapExtension
    )
}

pub fn reconnect_delay(reconnects_used: usize) -> Duration {
    Duration::from_secs((1_u64 << reconnects_used.min(5)).min(30))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn update(address: &str, slot: u64, byte: u8) -> RawAccountUpdate {
        RawAccountUpdate {
            address: address.into(),
            subscription_id: 1,
            slot,
            owner: "owner".into(),
            data: vec![byte],
        }
    }

    #[test]
    fn monitor_config_parses_bounds_and_deadline_wait() {
        let config =
            OpportunityMonitorConfig::parse(Some("2"), Some("90"), Some("45"), Some("3")).unwrap();
        assert_eq!(config.target_updates, Some(2));
        assert_eq!(config.max_duration, Some(Duration::from_secs(90)));
        assert_eq!(
            config.wait_timeout(Duration::from_secs(10)),
            Some(Duration::from_secs(45))
        );
        assert_eq!(
            config.wait_timeout(Duration::from_secs(80)),
            Some(Duration::from_secs(10))
        );
        assert_eq!(config.wait_timeout(Duration::from_secs(90)), None);
        assert!(!config.target_reached(1));
        assert!(config.target_reached(2));
        assert!(config.reconnect_allowed(2));
        assert!(!config.reconnect_allowed(3));
        assert!(OpportunityMonitorConfig::parse(Some("0"), None, None, None).is_err());
        assert!(OpportunityMonitorConfig::parse(None, Some("0"), None, None).is_err());
        assert!(OpportunityMonitorConfig::parse(None, None, Some("0"), None).is_err());
    }

    #[test]
    fn watermark_rejects_exact_duplicates_and_stale_but_accepts_same_slot_changes() {
        let mut watermark = UpdateWatermark::default();
        assert_eq!(watermark.classify(&update("a", 10, 1)), UpdateNovelty::New);
        assert_eq!(
            watermark.classify(&update("a", 10, 1)),
            UpdateNovelty::Duplicate
        );
        assert_eq!(watermark.classify(&update("a", 9, 2)), UpdateNovelty::Stale);
        assert_eq!(watermark.classify(&update("a", 10, 2)), UpdateNovelty::New);
        assert_eq!(watermark.classify(&update("a", 11, 2)), UpdateNovelty::New);
        assert_eq!(watermark.classify(&update("b", 1, 9)), UpdateNovelty::New);
    }

    #[test]
    fn subscription_set_comparison_ignores_order_but_not_membership() {
        assert!(subscription_sets_equal(
            &["a".into(), "b".into()],
            &["b".into(), "a".into()]
        ));
        assert!(!subscription_sets_equal(
            &["a".into(), "b".into()],
            &["a".into(), "c".into()]
        ));
        assert!(!subscription_sets_equal(
            &["a".into()],
            &["a".into(), "b".into()]
        ));
    }

    #[test]
    fn dependency_refresh_and_reconnect_backoff_are_narrow() {
        assert!(dependency_update_may_change_set(DependencyKind::PoolState));
        assert!(dependency_update_may_change_set(
            DependencyKind::BitmapExtension
        ));
        assert!(!dependency_update_may_change_set(
            DependencyKind::TokenVault
        ));
        assert!(!dependency_update_may_change_set(DependencyKind::TickArray));
        assert!(!dependency_update_may_change_set(DependencyKind::Oracle));
        assert!(!dependency_update_may_change_set(DependencyKind::BinArray));
        assert!(!dependency_update_may_change_set(DependencyKind::TokenMint));
        assert_eq!(reconnect_delay(0), Duration::from_secs(1));
        assert_eq!(reconnect_delay(3), Duration::from_secs(8));
        assert_eq!(reconnect_delay(10), Duration::from_secs(30));
    }
}
