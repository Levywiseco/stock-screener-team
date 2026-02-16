"""
A股三合一多策略选股器 - GitHub Actions 云端版本
策略: 三日反转 | 放量突破 | 缩量突破
数据源: BaoStock (全市场股票列表 + 日K线) + 新浪财经 (最新名称)
"""

import baostock as bs
import pandas as pd
from datetime import datetime, timedelta
import urllib.request
import re
import warnings
import time
import os
import json

warnings.filterwarnings('ignore')


class CombinedScreenerCloud:
    """三合一多策略选股器（云端版）"""

    def __init__(self):
        self.bs_logged_in = False
        self.history_days = 200  # 取三个策略中最大值（放量突破需要200天）

        # 策略1: 三日反转 配置
        self.reversal_config = {
            'small_bear_min': 0.3,
            'small_bear_max': 2.0,
            'big_bear_min': 3.0,
            'gap_down_min': 0,
            'bull_close_min': 1.0,
            'prior_decline_days': 22,
            'prior_decline_min': 3,
            'max_upper_shadow': 30,
        }

        # 策略2: 放量突破 配置
        self.volume_breakout_config = {
            'prior_lookback_days': 60,
            'prior_decline_min_pct': 15,
            'consol_min_days': 22,
            'consol_max_range_pct': 15,
            'limit_up_main_board': 9.5,
            'limit_up_chinext': 19.5,
            'limit_up_body_ratio_min': 0.6,
            'post_consol_min_days': 5,
            'post_consol_max_lookback': 20,
        }

        # 策略3: 缩量突破 配置
        self.shrink_breakout_config = {
            'min_decline_pct': 15,
            'consolidation1_min_days': 22,
            'consolidation1_max_amplitude': 15,
            'limit_up_main_pct': 9.8,
            'limit_up_chinext_pct': 19.8,
            'consolidation2_min_days': 5,
            'consolidation2_max_amplitude': 10,
            'consolidation2_support_tolerance': 0,
            'vol_ma_short': 5,
            'vol_ma_long': 10,
        }

    # ==================== 数据获取层 ====================

    def login(self):
        """BaoStock 会话登录"""
        if not self.bs_logged_in:
            lg = bs.login()
            if lg.error_code == '0':
                self.bs_logged_in = True
                return True
            else:
                print(f"BaoStock登录失败: {lg.error_msg}")
                return False
        return True

    def logout(self):
        """BaoStock 会话登出"""
        if self.bs_logged_in:
            bs.logout()
            self.bs_logged_in = False

    def get_stock_list(self):
        """BaoStock获取全市场股票，排除ST/科创板/北交所"""
        print("正在获取A股列表...")
        try:
            rs = bs.query_stock_basic()
            stocks = []

            while rs.error_code == '0' and rs.next():
                row = rs.get_row_data()
                code = row[0]      # sh.600000
                name = row[1]
                status = row[4]    # 1=上市

                if status != '1':
                    continue
                if 'ST' in name or '退' in name:
                    continue
                if code.startswith('sh.688') or code.startswith('sh.689'):
                    continue
                if code.startswith('bj.'):
                    continue
                if code.startswith('sh.6') or code.startswith('sz.0') or code.startswith('sz.3'):
                    stocks.append({
                        'bs_code': code,
                        'code': code.split('.')[1],
                        'name': name
                    })

            print(f"共获取 {len(stocks)} 只股票")
            return stocks
        except Exception as e:
            print(f"获取股票列表失败: {e}")
            return []

    def get_stock_history(self, bs_code):
        """BaoStock获取前复权日K线（200交易日）"""
        try:
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=self.history_days * 2)).strftime('%Y-%m-%d')

            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2"  # 前复权
            )

            if rs.error_code != '0':
                return None

            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())

            if len(data_list) < 30:
                return None

            df = pd.DataFrame(data_list, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna()

            if len(df) < 30:
                return None

            return df.tail(self.history_days).reset_index(drop=True)
        except Exception:
            return None

    def get_latest_names(self, codes):
        """新浪财经API获取最新名称（失败时降级用BaoStock名称）"""
        if not codes:
            return {}
        name_map = {}
        try:
            sina_codes = []
            for code in codes:
                prefix = 'sh' if code.startswith('6') else 'sz'
                sina_codes.append(f'{prefix}{code}')

            batch_size = 100
            for i in range(0, len(sina_codes), batch_size):
                batch = sina_codes[i:i+batch_size]
                url = 'http://hq.sinajs.cn/list=' + ','.join(batch)
                req = urllib.request.Request(url, headers={'Referer': 'http://finance.sina.com.cn'})
                response = urllib.request.urlopen(req, timeout=10)
                content = response.read().decode('gbk')

                for line in content.strip().split('\n'):
                    match = re.search(r'hq_str_(\w+)="([^,]+)', line)
                    if match:
                        sina_code = match.group(1)
                        name = match.group(2)
                        pure_code = sina_code[2:]
                        name_map[pure_code] = name
        except Exception as e:
            print(f"获取最新名称失败: {e}")
        return name_map

    # ==================== 策略1: 三日反转 ====================

    def check_reversal_pattern(self, df):
        """三日反转形态检测: 小阴线 + 大阴线 + 低开高收阳线"""
        cfg = self.reversal_config
        min_required = 3 + cfg['prior_decline_days']
        if df is None or len(df) < min_required:
            return False, None

        day1 = df.iloc[-3]
        day2 = df.iloc[-2]
        day3 = df.iloc[-1]

        # 检查形态前的累计跌幅
        prior_days = cfg['prior_decline_days']
        prior_start_idx = -3 - prior_days
        if abs(prior_start_idx) > len(df):
            return False, None

        prior_start_close = df.iloc[prior_start_idx]['close']
        prior_end_close = df.iloc[-4]['close']

        if prior_start_close == 0:
            return False, None

        prior_decline = (prior_end_close - prior_start_close) / prior_start_close * 100

        if cfg['prior_decline_min'] > 0:
            if prior_decline >= 0 or abs(prior_decline) < cfg['prior_decline_min']:
                return False, None

        if day1['open'] == 0 or day2['open'] == 0 or day3['open'] == 0:
            return False, None
        if day2['close'] == 0:
            return False, None

        # 第一天：小阴线
        day1_change = (day1['close'] - day1['open']) / day1['open'] * 100
        is_day1_bear = day1['close'] < day1['open']
        is_day1_small = cfg['small_bear_min'] <= abs(day1_change) <= cfg['small_bear_max']

        # 第二天：大阴线
        day2_change = (day2['close'] - day2['open']) / day2['open'] * 100
        is_day2_bear = day2['close'] < day2['open']
        is_day2_big = abs(day2_change) >= cfg['big_bear_min']

        # 第三天：低开高收阳线
        gap_down = (day3['open'] - day2['close']) / day2['close'] * 100
        day3_change = (day3['close'] - day3['open']) / day3['open'] * 100
        is_day3_gap_down = gap_down <= -cfg['gap_down_min']
        is_day3_bull = day3['close'] > day3['open']
        is_day3_strong = day3_change >= cfg['bull_close_min']

        day1_not_engulfed = not (day2['high'] >= day1['high'] and day2['low'] <= day1['low'])
        day2_gap_down = (day2['open'] - day1['close']) / day1['close'] * 100

        def upper_shadow_ratio(row):
            body_top = max(row['open'], row['close'])
            total_range = row['high'] - row['low']
            if total_range == 0:
                return 0
            upper_shadow = row['high'] - body_top
            return upper_shadow / total_range * 100

        day1_upper_shadow = upper_shadow_ratio(day1)
        day2_upper_shadow = upper_shadow_ratio(day2)
        day3_upper_shadow = upper_shadow_ratio(day3)
        max_upper_shadow = max(day1_upper_shadow, day2_upper_shadow, day3_upper_shadow)
        upper_shadow_ok = max_upper_shadow <= cfg['max_upper_shadow']

        if abs(day1_change) > 0:
            bear_contrast = abs(day2_change) / abs(day1_change)
        else:
            bear_contrast = 0

        pattern_match = (
            is_day1_bear and is_day1_small and
            is_day2_bear and is_day2_big and
            is_day3_gap_down and is_day3_bull and is_day3_strong and
            upper_shadow_ok
        )

        if pattern_match:
            score = 40
            score += min(abs(day2_change) - 3, 5) * 2
            score += min(abs(gap_down), 3) * 2
            if day1_not_engulfed:
                score += 8
            if day2_gap_down < 0:
                score += min(abs(day2_gap_down), 2) * 5
            if day3_change <= 3:
                score += 8
            elif day3_change <= 5:
                score += 4
            score += min(bear_contrast - 1.5, 3) * 3.33
            if max_upper_shadow <= 10:
                score += 8
            elif max_upper_shadow <= 20:
                score += 5
            elif max_upper_shadow <= 30:
                score += 2
            score = min(100, max(0, score))

            detail = {
                'day1_change': round(day1_change, 2),
                'day2_change': round(day2_change, 2),
                'day3_change': round(day3_change, 2),
                'gap_down': round(gap_down, 2),
                'day2_gap': round(day2_gap_down, 2),
                'contrast': round(bear_contrast, 1),
                'shadow': round(max_upper_shadow, 1),
                'engulfed': not day1_not_engulfed,
                'prior_decline': round(prior_decline, 2),
                'day1_date': str(day1['date']),
                'day2_date': str(day2['date']),
                'day3_date': str(day3['date']),
                'score': round(score)
            }
            return True, detail

        return False, None

    # ==================== 策略2: 放量突破 ====================

    def _vb_is_limit_up(self, row, prev_close, code):
        """放量突破-判断是否为涨停板大阳线"""
        cfg = self.volume_breakout_config
        if prev_close == 0:
            return False

        change_pct = (row['close'] - prev_close) / prev_close * 100

        if code.startswith('3'):
            threshold = cfg['limit_up_chinext']
        else:
            threshold = cfg['limit_up_main_board']

        if change_pct < threshold:
            return False

        # 实体占比: 排除一字板
        total_range = row['high'] - row['low']
        if total_range == 0:
            return False
        body = abs(row['close'] - row['open'])
        body_ratio = body / total_range

        if body_ratio < cfg['limit_up_body_ratio_min']:
            return False

        # 必须是阳线
        if row['close'] <= row['open']:
            return False

        return True

    def _vb_find_consolidation(self, df, limit_up_idx):
        """放量突破-涨停板前寻找止跌横盘区间"""
        cfg = self.volume_breakout_config
        consol_min_days = cfg['consol_min_days']
        consol_max_range = cfg['consol_max_range_pct']

        consol_end = limit_up_idx - 1
        if consol_end < consol_min_days:
            return None

        best = None
        for length in range(consol_min_days, min(consol_end + 1, 80)):
            consol_start = consol_end - length + 1
            if consol_start < 0:
                break

            window = df.iloc[consol_start:consol_end + 1]
            closes = window['close']
            mean_price = closes.mean()
            if mean_price == 0:
                continue

            range_pct = (closes.max() - closes.min()) / mean_price * 100

            if range_pct <= consol_max_range:
                best = (consol_start, consol_end, mean_price, range_pct, length)
            else:
                if best is not None:
                    break

        return best

    def _vb_check_prior_decline(self, df, consol_start, consol_mean):
        """放量突破-检查横盘前的前期下跌"""
        cfg = self.volume_breakout_config
        lookback = cfg['prior_lookback_days']
        min_decline = cfg['prior_decline_min_pct']

        search_start = max(0, consol_start - lookback)
        if search_start >= consol_start:
            return None

        prior_slice = df.iloc[search_start:consol_start]
        if len(prior_slice) == 0:
            return None

        prior_high = prior_slice['close'].max()

        if consol_mean == 0:
            return None

        decline_pct = (prior_high - consol_mean) / consol_mean * 100

        if decline_pct < min_decline:
            return None

        return (decline_pct, prior_high)

    def _vb_calculate_score(self, detail):
        """放量突破-7维评分（满分100分）"""
        score = 0

        # 1. 前期跌幅深度 (15分)
        score += self._linear_map(detail['prior_decline_pct'], 15, 40, 0, 15)

        # 2. 横盘时间长度 (10分)
        score += self._linear_map(detail['consol_days'], 22, 60, 0, 10)

        # 3. 涨停K线质量 (20分)
        score += self._linear_map(detail['limit_up_body_ratio'], 0.6, 1.0, 0, 10)
        score += self._linear_map(detail['limit_up_vol_ratio'], 1, 4, 0, 10)

        # 4. 回踩横盘紧凑度 (15分)
        if detail['post_range_pct'] <= 5:
            score += 15
        elif detail['post_range_pct'] <= 10:
            score += self._linear_map(detail['post_range_pct'], 10, 5, 0, 15)

        # 5. 回踩持续天数 (10分)
        score += self._linear_map(detail['post_consol_days'], 5, 15, 0, 10)

        # 6. 突破放量倍数 (15分)
        score += self._linear_map(detail['break_vol_ratio'], 1, 3, 0, 15)

        # 7. 突破阳线实体 (15分)
        score += self._linear_map(detail['break_body_ratio'], 0.3, 0.9, 0, 15)

        return round(min(100, max(0, score)))

    @staticmethod
    def _linear_map(value, low, high, score_low, score_high):
        """线性映射: value在[low, high]区间映射到[score_low, score_high]"""
        if high == low:
            return score_high if value >= high else score_low
        ratio = (value - low) / (high - low)
        ratio = max(0, min(1, ratio))
        return score_low + ratio * (score_high - score_low)

    def check_volume_breakout_pattern(self, df, code):
        """放量突破形态检测: 前期下跌 → 止跌横盘 → 涨停板 → 回踩横盘 → 放量突破"""
        cfg = self.volume_breakout_config
        if df is None or len(df) < 50:
            return False, None

        n = len(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # 最后一天必须是阳线
        if last['close'] <= last['open']:
            return False, None
        # 必须放量（成交量 > 前一日）
        if prev['volume'] == 0 or last['volume'] <= prev['volume']:
            return False, None

        # 向前扫描涨停板，并验证回踩横盘
        max_lookback = min(cfg['post_consol_max_lookback'], n - 30)
        found_pattern = False
        best_detail = None

        for scan_idx in range(2, max_lookback + 1):
            candidate_idx = n - 1 - scan_idx
            if candidate_idx < 1:
                break

            candidate = df.iloc[candidate_idx]
            prev_of_candidate = df.iloc[candidate_idx - 1]

            if not self._vb_is_limit_up(candidate, prev_of_candidate['close'], code):
                continue

            # 回踩区间: 涨停板次日 到 突破日前一日
            post_start = candidate_idx + 1
            post_end = n - 2

            post_days = post_end - post_start + 1
            if post_days < cfg['post_consol_min_days']:
                continue

            post_slice = df.iloc[post_start:post_end + 1]
            limit_up_open = candidate['open']
            limit_up_close = candidate['close']

            # 每天收盘价 >= 涨停板开盘价
            if (post_slice['close'] < limit_up_open).any():
                continue

            # 突破日收盘价 > 涨停板收盘价
            if last['close'] <= limit_up_close:
                continue

            # 涨停板前的止跌横盘
            consol_result = self._vb_find_consolidation(df, candidate_idx)
            if consol_result is None:
                continue

            consol_start, consol_end, consol_mean, consol_range_pct, consol_days = consol_result

            # 横盘前的前期下跌
            decline_result = self._vb_check_prior_decline(df, consol_start, consol_mean)
            if decline_result is None:
                continue

            prior_decline_pct, prior_high = decline_result

            # 形态匹配成功，计算详细信息
            limit_up_change = (candidate['close'] - prev_of_candidate['close']) / prev_of_candidate['close'] * 100
            body = abs(candidate['close'] - candidate['open'])
            total_range = candidate['high'] - candidate['low']
            limit_up_body_ratio = body / total_range if total_range > 0 else 0

            post_closes = post_slice['close']
            post_mean = post_closes.mean()
            post_range_pct = (post_closes.max() - post_closes.min()) / post_mean * 100 if post_mean > 0 else 999

            # 涨停日量比
            vol_start = max(0, candidate_idx - 5)
            prior_avg_vol = df.iloc[vol_start:candidate_idx]['volume'].mean()
            limit_up_vol_ratio = candidate['volume'] / prior_avg_vol if prior_avg_vol > 0 else 1

            # 突破日量比
            break_vol_ratio = last['volume'] / prev['volume'] if prev['volume'] > 0 else 1

            # 突破阳线实体占比
            break_total_range = last['high'] - last['low']
            break_body = last['close'] - last['open']
            break_body_ratio = break_body / break_total_range if break_total_range > 0 else 0

            detail = {
                'prior_decline_pct': round(prior_decline_pct, 2),
                'consol_days': consol_days,
                'consol_range_pct': round(consol_range_pct, 2),
                'consol_mean_price': round(consol_mean, 2),
                'limit_up_date': str(candidate['date']),
                'limit_up_change': round(limit_up_change, 2),
                'limit_up_body_ratio': round(limit_up_body_ratio, 2),
                'limit_up_vol_ratio': round(limit_up_vol_ratio, 2),
                'post_consol_days': post_days,
                'post_range_pct': round(post_range_pct, 2),
                'break_date': str(last['date']),
                'break_change': round((last['close'] - prev['close']) / prev['close'] * 100, 2),
                'break_vol_ratio': round(break_vol_ratio, 2),
                'break_body_ratio': round(break_body_ratio, 2),
                'break_close': round(last['close'], 2),
            }

            detail['score'] = self._vb_calculate_score(detail)

            if best_detail is None or detail['score'] > best_detail['score']:
                best_detail = detail

            found_pattern = True

        if found_pattern and best_detail:
            return True, best_detail
        return False, None

    # ==================== 策略3: 缩量突破 ====================

    def _sb_is_limit_up(self, close, prev_close, code):
        """缩量突破-判断是否涨停"""
        cfg = self.shrink_breakout_config
        if prev_close == 0:
            return False
        change_pct = (close - prev_close) / prev_close * 100
        if code.startswith('300'):
            return change_pct >= cfg['limit_up_chinext_pct']
        else:
            return change_pct >= cfg['limit_up_main_pct']

    def _sb_find_limit_up_day(self, df, code):
        """缩量突破-从今日向前搜索最近的涨停日，且距今至少6个交易日"""
        n = len(df)
        for i in range(n - 7, 0, -1):
            if self._sb_is_limit_up(df.iloc[i]['close'], df.iloc[i - 1]['close'], code):
                return i
        return None

    def _sb_check_post_consolidation(self, df, limit_up_idx):
        """缩量突破-检查涨停后横盘"""
        cfg = self.shrink_breakout_config
        n = len(df)
        signal_idx = n - 1
        start = limit_up_idx + 1
        end = signal_idx

        if end - start < cfg['consolidation2_min_days']:
            return False, None

        post_df = df.iloc[start:end]
        limit_up_close = df.iloc[limit_up_idx]['close']

        # 所有K线收盘价严格>=涨停日收盘价
        tolerance = limit_up_close * cfg['consolidation2_support_tolerance'] / 100
        if (post_df['close'] < limit_up_close - tolerance).any():
            return False, None

        # 振幅控制
        max_close = post_df['close'].max()
        min_close = post_df['close'].min()
        avg_close = post_df['close'].mean()
        if avg_close == 0:
            return False, None
        amplitude = (max_close - min_close) / avg_close * 100
        if amplitude > cfg['consolidation2_max_amplitude']:
            return False, None

        # 今日收盘价必须突破横盘区间所有收盘价
        signal_close = df.iloc[signal_idx]['close']
        if signal_close <= max_close:
            return False, None

        detail = {
            'post_days': end - start,
            'post_amplitude': round(amplitude, 2),
            'post_max_close': max_close,
            'post_min_close': min_close,
        }
        return True, detail

    def _sb_check_pre_consolidation(self, df, limit_up_idx, code):
        """缩量突破-检查涨停前横盘"""
        cfg = self.shrink_breakout_config
        min_days = cfg['consolidation1_min_days']
        max_amp = cfg['consolidation1_max_amplitude']

        if limit_up_idx < min_days:
            return False, None

        best_start = None
        best_end = limit_up_idx

        for start in range(limit_up_idx - min_days, -1, -1):
            segment = df.iloc[start:best_end]
            seg_max = segment['close'].max()
            seg_min = segment['close'].min()
            seg_avg = segment['close'].mean()
            if seg_avg == 0:
                break
            amp = (seg_max - seg_min) / seg_avg * 100
            if amp <= max_amp:
                best_start = start
            else:
                break

        if best_start is None:
            return False, None

        consolidation_days = best_end - best_start
        if consolidation_days < min_days:
            return False, None

        # 检查横盘区间内是否有其他涨停板
        for i in range(best_start + 1, best_end):
            if self._sb_is_limit_up(df.iloc[i]['close'], df.iloc[i - 1]['close'], code):
                return False, None

        detail = {
            'consolidation1_start': best_start,
            'consolidation1_days': consolidation_days,
            'consolidation1_amplitude': round(
                (df.iloc[best_start:best_end]['close'].max() - df.iloc[best_start:best_end]['close'].min())
                / df.iloc[best_start:best_end]['close'].mean() * 100, 2
            ),
        }
        return True, detail

    def _sb_check_prior_decline(self, df, consolidation_start):
        """缩量突破-检查横盘前是否有显著下跌"""
        cfg = self.shrink_breakout_config
        lookback = min(consolidation_start, 60)
        if lookback < 5:
            return False, None

        search_start = consolidation_start - lookback
        search_df = df.iloc[search_start:consolidation_start]

        max_close = search_df['close'].max()
        end_close = df.iloc[consolidation_start]['close']

        if max_close == 0:
            return False, None

        decline_pct = (max_close - end_close) / max_close * 100

        if decline_pct < cfg['min_decline_pct']:
            return False, None

        detail = {
            'decline_pct': round(decline_pct, 2),
        }
        return True, detail

    def _sb_check_signal(self, df):
        """缩量突破-检查今日信号条件：阳线 + 量能死叉"""
        cfg = self.shrink_breakout_config
        n = len(df)
        signal_idx = n - 1
        row = df.iloc[signal_idx]

        # 今日是阳线
        if row['close'] <= row['open']:
            return False, None

        # 计算均量线
        ma_short_period = cfg['vol_ma_short']
        ma_long_period = cfg['vol_ma_long']

        if n < ma_long_period:
            return False, None

        vol_ma_short = df['volume'].iloc[n - ma_short_period:n].mean()
        vol_ma_long = df['volume'].iloc[n - ma_long_period:n].mean()

        if vol_ma_long == 0:
            return False, None

        # 5日均量 < 10日均量（死叉状态）
        if vol_ma_short >= vol_ma_long:
            return False, None

        signal_change = (row['close'] - row['open']) / row['open'] * 100
        vol_ratio = vol_ma_short / vol_ma_long

        detail = {
            'signal_change': round(signal_change, 2),
            'vol_ma_short': round(vol_ma_short, 0),
            'vol_ma_long': round(vol_ma_long, 0),
            'vol_ratio': round(vol_ratio, 4),
            'signal_date': str(row['date']),
        }
        return True, detail

    def _sb_calculate_score(self, details):
        """缩量突破-评分（基础分40，满分100）"""
        score = 40

        # 第一次横盘越长: +0~10
        extra_days = details['consolidation1_days'] - 22
        if extra_days > 0:
            score += min(int(extra_days / 10) * 3, 10)

        # 前期跌幅越深: +0~10
        extra_decline = details['decline_pct'] - 15
        if extra_decline > 0:
            score += min(int(extra_decline / 5) * 3, 10)

        # 涨停力度: +0~8
        if details['limit_up_type'] in ('一字板', 'T字板'):
            score += 8
        else:
            score += 5

        # 第二次横盘越紧: +0~8
        amp = details['post_amplitude']
        if amp < 5:
            score += 8
        elif amp < 8:
            score += 5
        elif amp < 10:
            score += 3

        # 量能萎缩程度: +0~10
        vol_ratio = details['vol_ratio']
        if vol_ratio < 0.5:
            score += 10
        elif vol_ratio < 0.6:
            score += 8
        elif vol_ratio < 0.7:
            score += 6
        elif vol_ratio < 0.8:
            score += 4
        elif vol_ratio < 0.9:
            score += 2

        # 突破阳线幅度适中: +0~8
        change = details['signal_change']
        if 2 <= change <= 5:
            score += 8
        elif 5 < change <= 8:
            score += 5

        # 涨停后无破位: +6
        score += 6

        return min(100, max(0, round(score)))

    def check_shrink_breakout_pattern(self, df, code):
        """缩量突破形态检测: 下跌 → 横盘 → 涨停 → 横盘 → 缩量突破"""
        if df is None or len(df) < 60:
            return False, None

        # Phase 1: 检查今日信号条件
        signal_ok, signal_detail = self._sb_check_signal(df)
        if not signal_ok:
            return False, None

        # Phase 2: 寻找最近的涨停板
        limit_up_idx = self._sb_find_limit_up_day(df, code)
        if limit_up_idx is None:
            return False, None

        # Phase 3: 检查涨停后横盘
        post_ok, post_detail = self._sb_check_post_consolidation(df, limit_up_idx)
        if not post_ok:
            return False, None

        # Phase 4: 检查涨停前横盘
        pre_ok, pre_detail = self._sb_check_pre_consolidation(df, limit_up_idx, code)
        if not pre_ok:
            return False, None

        # Phase 5: 检查横盘前下跌
        decline_ok, decline_detail = self._sb_check_prior_decline(df, pre_detail['consolidation1_start'])
        if not decline_ok:
            return False, None

        # 涨停日信息
        limit_up_row = df.iloc[limit_up_idx]
        prev_close = df.iloc[limit_up_idx - 1]['close']
        limit_up_change = (limit_up_row['close'] - prev_close) / prev_close * 100

        # 涨停形态判断
        if limit_up_row['open'] == limit_up_row['close'] == limit_up_row['high']:
            limit_up_type = '一字板'
        elif limit_up_row['open'] == limit_up_row['low'] and limit_up_row['close'] == limit_up_row['high']:
            limit_up_type = 'T字板'
        else:
            limit_up_type = '普通涨停'

        # 汇总详情
        detail = {
            **signal_detail,
            **post_detail,
            **pre_detail,
            **decline_detail,
            'limit_up_date': str(limit_up_row['date']),
            'limit_up_change': round(limit_up_change, 2),
            'limit_up_type': limit_up_type,
        }

        detail['score'] = self._sb_calculate_score(detail)

        return True, detail

    # ==================== 筛选层 ====================

    def screen_single_stock(self, bs_code, code, name):
        """一次获取K线，串行调用3个check_pattern"""
        df = self.get_stock_history(bs_code)
        if df is None:
            return None

        results = {}

        # 策略1: 三日反转（check_pattern只需df）
        try:
            match, detail = self.check_reversal_pattern(df)
            if match:
                results['reversal'] = {'code': code, 'name': name, **detail}
        except Exception:
            pass

        # 策略2: 放量突破（check_pattern需要df和code）
        try:
            match, detail = self.check_volume_breakout_pattern(df, code)
            if match:
                results['volume_breakout'] = {'code': code, 'name': name, **detail}
        except Exception:
            pass

        # 策略3: 缩量突破（check_pattern需要df和code）
        try:
            match, detail = self.check_shrink_breakout_pattern(df, code)
            if match:
                results['shrink_breakout'] = {'code': code, 'name': name, **detail}
        except Exception:
            pass

        return results if results else None

    # ==================== 主流程 ====================

    def run(self):
        """运行三合一筛选"""
        print("=" * 70)
        print("A股三合一多策略选股器 (GitHub Actions 云端版)")
        print("策略: 三日反转 | 放量突破 | 缩量突破")
        print("数据源: BaoStock (串行遍历)")
        print("=" * 70)

        start_time = time.time()

        if not self.login():
            return

        try:
            stocks = self.get_stock_list()
            if not stocks:
                print("获取股票列表失败，退出")
                return

            print(f"\n开始筛选（串行遍历 {len(stocks)} 只股票）...")
            reversal_results = []
            volume_results = []
            shrink_results = []
            total = len(stocks)

            for i, stock in enumerate(stocks):
                if (i + 1) % 200 == 0:
                    print(f"进度: {i+1}/{total} ({(i+1)*100//total}%)")
                try:
                    result = self.screen_single_stock(stock['bs_code'], stock['code'], stock['name'])
                    if result:
                        hits = []
                        if 'reversal' in result:
                            reversal_results.append(result['reversal'])
                            hits.append('三日反转')
                        if 'volume_breakout' in result:
                            volume_results.append(result['volume_breakout'])
                            hits.append('放量突破')
                        if 'shrink_breakout' in result:
                            shrink_results.append(result['shrink_breakout'])
                            hits.append('缩量突破')
                        if hits:
                            print(f"  命中: {stock['code']} {stock['name']} [{', '.join(hits)}]")
                except Exception:
                    pass

            # 按评分排序
            reversal_results.sort(key=lambda x: x.get('score', 0), reverse=True)
            volume_results.sort(key=lambda x: x.get('score', 0), reverse=True)
            shrink_results.sort(key=lambda x: x.get('score', 0), reverse=True)

            # 获取最新名称
            all_codes = set()
            for r in reversal_results + volume_results + shrink_results:
                all_codes.add(r['code'])

            if all_codes:
                print("\n正在获取最新股票名称...")
                name_map = self.get_latest_names(list(all_codes))
                for r in reversal_results + volume_results + shrink_results:
                    if r['code'] in name_map:
                        r['name'] = name_map[r['code']]

            elapsed = time.time() - start_time

            # 保存结果
            self._save_results(reversal_results, volume_results, shrink_results, total, elapsed)

        finally:
            self.logout()

    # ==================== 输出层 ====================

    def _save_results(self, reversal_results, volume_results, shrink_results, total_scanned, elapsed):
        """保存结果到CSV/JSON/Markdown + GitHub Actions Summary"""
        output_dir = os.environ.get('OUTPUT_DIR', '.')
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        # 保存 CSV 文件（每个策略各一个）
        if reversal_results:
            df = pd.DataFrame(reversal_results)
            csv_path = os.path.join(output_dir, f"reversal_{timestamp}.csv")
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            print(f"\n三日反转 CSV: {csv_path}")

        if volume_results:
            df = pd.DataFrame(volume_results)
            csv_path = os.path.join(output_dir, f"volume_breakout_{timestamp}.csv")
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            print(f"放量突破 CSV: {csv_path}")

        if shrink_results:
            df = pd.DataFrame(shrink_results)
            csv_path = os.path.join(output_dir, f"shrink_breakout_{timestamp}.csv")
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            print(f"缩量突破 CSV: {csv_path}")

        # 保存 JSON（供 Claude Agent 读取）
        json_data = {
            'timestamp': timestamp,
            'total_scanned': total_scanned,
            'elapsed_seconds': round(elapsed, 1),
            'reversal': {
                'count': len(reversal_results),
                'results': reversal_results
            },
            'volume_breakout': {
                'count': len(volume_results),
                'results': volume_results
            },
            'shrink_breakout': {
                'count': len(shrink_results),
                'results': shrink_results
            }
        }
        json_path = os.path.join(output_dir, "latest_results.json")
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)
        print(f"JSON 结果: {json_path}")

        # 保存 Markdown 报告
        md_content = self._format_markdown(
            reversal_results, volume_results, shrink_results, total_scanned, elapsed
        )
        md_path = os.path.join(output_dir, "results.md")
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"Markdown 报告: {md_path}")

        # 写入 GitHub Actions Summary
        github_step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if github_step_summary:
            with open(github_step_summary, 'a', encoding='utf-8') as f:
                f.write(md_content)
            print("已写入 GitHub Actions Summary")

        # 打印终端汇总
        print("\n" + "=" * 70)
        print(f"筛选完成！耗时: {elapsed:.1f}秒")
        print(f"扫描股票: {total_scanned} 只")
        print(f"三日反转: {len(reversal_results)} 只 | "
              f"放量突破: {len(volume_results)} 只 | "
              f"缩量突破: {len(shrink_results)} 只")
        print("=" * 70)

    def _format_markdown(self, reversal_results, volume_results, shrink_results,
                         total_scanned, elapsed):
        """格式化Markdown报告"""
        md = f"## A股三合一多策略选股结果\n\n"
        md += f"**筛选时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        md += f"**扫描股票**: {total_scanned} 只 | **耗时**: {elapsed:.1f}秒\n\n"

        # 总览表
        md += "### 总览\n\n"
        md += "| 策略 | 命中数 |\n|------|--------|\n"
        md += f"| 三日反转 | {len(reversal_results)} |\n"
        md += f"| 放量突破 | {len(volume_results)} |\n"
        md += f"| 缩量突破 | {len(shrink_results)} |\n\n"

        # 交叉验证: 多策略共振
        hit_map = {}
        for r in reversal_results:
            hit_map.setdefault(r['code'], {'name': r['name'], 'strategies': [], 'scores': {}})
            hit_map[r['code']]['strategies'].append('三日反转')
            hit_map[r['code']]['scores']['三日反转'] = r.get('score', 0)
        for r in volume_results:
            hit_map.setdefault(r['code'], {'name': r['name'], 'strategies': [], 'scores': {}})
            hit_map[r['code']]['strategies'].append('放量突破')
            hit_map[r['code']]['scores']['放量突破'] = r.get('score', 0)
        for r in shrink_results:
            hit_map.setdefault(r['code'], {'name': r['name'], 'strategies': [], 'scores': {}})
            hit_map[r['code']]['strategies'].append('缩量突破')
            hit_map[r['code']]['scores']['缩量突破'] = r.get('score', 0)

        multi_hit = {k: v for k, v in hit_map.items() if len(v['strategies']) >= 2}
        if multi_hit:
            md += "### 多策略共振\n\n"
            md += "| 代码 | 名称 | 命中策略 | 各策略评分 |\n"
            md += "|------|------|----------|------------|\n"
            for code, info in sorted(multi_hit.items(),
                                     key=lambda x: len(x[1]['strategies']), reverse=True):
                strategies = ', '.join(info['strategies'])
                scores = ', '.join(f"{s}:{info['scores'][s]}" for s in info['strategies'])
                md += f"| {code} | {info['name']} | {strategies} | {scores} |\n"
            md += "\n"

        # 策略一: 三日反转
        if reversal_results:
            md += "### 策略一: 三日反转\n\n"
            md += "| 代码 | 名称 | 前置跌幅 | 三日形态 | D2跳空 | 评分 |\n"
            md += "|------|------|----------|----------|--------|------|\n"
            for r in reversal_results:
                pattern = f"{r['day1_change']:+.1f}% → {r['day2_change']:+.1f}% → {r['day3_change']:+.1f}%"
                d2_gap = f"{r['day2_gap']:+.1f}%" if r['day2_gap'] < 0 else "无"
                md += f"| {r['code']} | {r['name']} | {r['prior_decline']:+.1f}% | {pattern} | {d2_gap} | {r['score']} |\n"
            md += "\n"

        # 策略二: 放量突破
        if volume_results:
            md += "### 策略二: 放量突破\n\n"
            md += "| 代码 | 名称 | 涨停日期 | 回踩天数 | 突破量比 | 评分 |\n"
            md += "|------|------|----------|----------|----------|------|\n"
            for r in volume_results:
                md += (f"| {r['code']} | {r['name']} | {r['limit_up_date']} | "
                       f"{r['post_consol_days']}天 | {r['break_vol_ratio']:.2f}x | {r['score']} |\n")
            md += "\n"

        # 策略三: 缩量突破
        if shrink_results:
            md += "### 策略三: 缩量突破\n\n"
            md += "| 代码 | 名称 | 涨停日期 | 涨停形态 | MA5/MA10 | 评分 |\n"
            md += "|------|------|----------|----------|----------|------|\n"
            for r in shrink_results:
                md += (f"| {r['code']} | {r['name']} | {r['limit_up_date']} | "
                       f"{r['limit_up_type']} | {r['vol_ratio']:.4f} | {r['score']} |\n")
            md += "\n"

        if not reversal_results and not volume_results and not shrink_results:
            md += "今日没有符合条件的股票。\n\n"

        md += "---\n*本工具仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。*\n"
        return md


def main():
    """主函数 - 支持 GitHub Actions 环境"""
    screener = CombinedScreenerCloud()
    screener.run()


if __name__ == "__main__":
    main()
