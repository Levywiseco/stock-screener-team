"""
A股三合一多策略选股器
合并三个独立策略为一次遍历，大幅减少HTTP请求数
策略: 三日反转 | 放量突破 | 缩量突破
数据源: AKShare (全市场行情 + 日K线) + 新浪财经 (最新名称)
"""

import akshare as ak
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import importlib.util
import urllib.request
import re
import warnings
import time
import sys
import os

warnings.filterwarnings('ignore')

# 修复Windows终端中文输出
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def import_class_from_file(file_path, module_name, class_name):
    """从文件动态导入类"""
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, class_name)


class CombinedScreener:
    """三合一多策略选股器"""

    def __init__(self):
        self.max_workers = 30
        self.history_days = 200  # 取三个策略中最大值（放量突破需要200天）

        # 动态导入三个策略类
        skills_dir = os.path.join(os.path.dirname(__file__), '..')

        ReversalScreener = import_class_from_file(
            os.path.join(skills_dir, 'stock-pattern-screener', 'stock_screener.py'),
            'reversal_module',
            'StockPatternScreener'
        )
        VolumeBreakoutScreener = import_class_from_file(
            os.path.join(skills_dir, 'consolidation-breakout-screener', 'consolidation_breakout_screener.py'),
            'volume_breakout_module',
            'ConsolidationBreakoutScreener'
        )
        ShrinkBreakoutScreener = import_class_from_file(
            os.path.join(skills_dir, 'stock-consolidation-breakout', 'stock_screener.py'),
            'shrink_breakout_module',
            'ConsolidationBreakoutScreener'
        )

        # 实例化（仅复用check_pattern逻辑，不使用其数据获取方法）
        self.reversal_screener = ReversalScreener()
        self.volume_screener = VolumeBreakoutScreener()
        self.shrink_screener = ShrinkBreakoutScreener()

    def get_stock_list(self):
        """使用AKShare全市场实时行情一次获取 + 预筛选"""
        print("正在获取全市场实时行情（ak.stock_zh_a_spot_em）...")
        try:
            df = ak.stock_zh_a_spot_em()
            total_market = len(df)
            print(f"全市场共 {total_market} 只股票")

            stocks = df.copy()

            # 排除ST和退市
            stocks = stocks[~stocks['名称'].str.contains('ST|退', na=False)]

            # 仅保留主板(60/00)和创业板(30)，排除科创板(688/689)和北交所
            stocks = stocks[stocks['代码'].str.match(r'^(6|0|3)\d{5}$')]
            stocks = stocks[~stocks['代码'].str.startswith('688')]
            stocks = stocks[~stocks['代码'].str.startswith('689')]

            after_exclude = len(stocks)
            print(f"排除ST/科创板/北交所后: {after_exclude} 只")

            # 预筛选：今日收阳（最新价 > 今开）—— 三个策略的共同必要条件
            latest = pd.to_numeric(stocks['最新价'], errors='coerce')
            today_open = pd.to_numeric(stocks['今开'], errors='coerce')
            stocks = stocks[(latest > today_open) & latest.notna() & today_open.notna()]

            print(f"今日收阳预筛选后: {len(stocks)} 只")

            result = []
            for _, row in stocks.iterrows():
                result.append({
                    'code': row['代码'],
                    'name': row['名称'],
                })

            return result
        except Exception as e:
            print(f"获取行情数据失败: {e}")
            return []

    def get_stock_history_ak(self, code):
        """使用AKShare获取单只股票历史K线数据（统一获取200天）"""
        try:
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=self.history_days * 2)).strftime('%Y%m%d')

            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"
            )

            if df is None or len(df) < 30:
                return None

            # 统一列名
            df = df.rename(columns={
                '日期': 'date', '开盘': 'open', '最高': 'high',
                '最低': 'low', '收盘': 'close', '成交量': 'volume'
            })
            df = df[['date', 'open', 'high', 'low', 'close', 'volume']].copy()

            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna()

            if len(df) < 30:
                return None

            return df.tail(self.history_days).reset_index(drop=True)
        except Exception:
            return None

    def get_latest_names(self, codes):
        """从新浪财经获取最新的股票名称"""
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
                batch = sina_codes[i:i + batch_size]
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

    def screen_single_stock(self, stock):
        """对单只股票运行全部3个策略（共享同一份K线数据）"""
        code = stock['code']
        name = stock['name']

        # 只获取一次K线数据
        df = self.get_stock_history_ak(code)
        if df is None:
            return None

        results = {}

        # 策略1: 三日反转（check_pattern只需df）
        try:
            match1, detail1 = self.reversal_screener.check_pattern(df)
            if match1:
                results['reversal'] = {'code': code, 'name': name, **detail1}
        except Exception:
            pass

        # 策略2: 放量突破（check_pattern需要df和code）
        try:
            match2, detail2 = self.volume_screener.check_pattern(df, code)
            if match2:
                results['volume_breakout'] = {'code': code, 'name': name, **detail2}
        except Exception:
            pass

        # 策略3: 缩量突破（check_pattern需要df和code）
        try:
            match3, detail3 = self.shrink_screener.check_pattern(df, code)
            if match3:
                results['shrink_breakout'] = {'code': code, 'name': name, **detail3}
        except Exception:
            pass

        return results if results else None

    def run(self):
        """运行三合一筛选"""
        print("=" * 70)
        print("A股三合一多策略选股器")
        print("策略: 三日反转 | 放量突破 | 缩量突破")
        print("优化: 统一数据获取 + 预筛选，一次遍历完成三策略筛选")
        print("=" * 70)

        start_time = time.time()

        # 第一步：获取预筛选后的股票列表
        stocks = self.get_stock_list()
        if not stocks:
            print("获取股票列表失败，退出")
            return

        # 第二步：多线程并发筛选
        print(f"\n开始筛选（{self.max_workers}线程并发）...")
        reversal_results = []
        volume_results = []
        shrink_results = []
        total = len(stocks)
        completed = 0

        strategy_names = {
            'reversal': '三日反转',
            'volume_breakout': '放量突破',
            'shrink_breakout': '缩量突破',
        }

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.screen_single_stock, stock): stock for stock in stocks}

            for future in as_completed(futures):
                completed += 1
                if completed % 200 == 0:
                    print(f"进度: {completed}/{total} ({completed * 100 // total}%)")
                try:
                    result = future.result()
                    if result:
                        stock = futures[future]
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

        # 保存CSV
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_files = {}

        if reversal_results:
            df_out = pd.DataFrame(reversal_results)
            f = f"D:/stock_pattern_{timestamp}.csv"
            df_out.to_csv(f, index=False, encoding='utf-8-sig')
            csv_files['reversal'] = f

        if volume_results:
            df_out = pd.DataFrame(volume_results)
            f = f"D:/consolidation_breakout_{timestamp}.csv"
            df_out.to_csv(f, index=False, encoding='utf-8-sig')
            csv_files['volume'] = f

        if shrink_results:
            df_out = pd.DataFrame(shrink_results)
            output_cols = [
                'code', 'name', 'decline_pct', 'consolidation1_days', 'consolidation1_amplitude',
                'limit_up_date', 'limit_up_change', 'limit_up_type',
                'post_days', 'post_amplitude',
                'vol_ratio', 'signal_change', 'signal_date', 'score'
            ]
            output_df = df_out[[c for c in output_cols if c in df_out.columns]]
            col_map = {
                'code': '代码', 'name': '名称', 'decline_pct': '前期跌幅%',
                'consolidation1_days': '横盘1天数', 'consolidation1_amplitude': '横盘1振幅%',
                'limit_up_date': '涨停日期', 'limit_up_change': '涨停幅度%',
                'limit_up_type': '涨停形态', 'post_days': '横盘2天数',
                'post_amplitude': '横盘2振幅%', 'vol_ratio': 'MA5/MA10',
                'signal_change': '信号涨幅%', 'signal_date': '信号日期', 'score': '评分'
            }
            output_df = output_df.rename(columns=col_map)
            f = f"D:/stock_consolidation_breakout_{timestamp}.csv"
            output_df.to_csv(f, index=False, encoding='utf-8-sig')
            csv_files['shrink'] = f

        # 输出汇总报告
        self._print_report(
            reversal_results, volume_results, shrink_results,
            csv_files, total, elapsed
        )

    def _print_report(self, reversal_results, volume_results, shrink_results,
                      csv_files, total_scanned, elapsed):
        """输出汇总报告"""

        # === 交叉验证：找出被多个策略命中的股票 ===
        hit_map = {}  # code -> {'name': ..., 'strategies': [...], 'scores': {...}}
        for r in reversal_results:
            code = r['code']
            if code not in hit_map:
                hit_map[code] = {'name': r['name'], 'strategies': [], 'scores': {}}
            hit_map[code]['strategies'].append('三日反转')
            hit_map[code]['scores']['三日反转'] = r.get('score', 0)
        for r in volume_results:
            code = r['code']
            if code not in hit_map:
                hit_map[code] = {'name': r['name'], 'strategies': [], 'scores': {}}
            hit_map[code]['strategies'].append('放量突破')
            hit_map[code]['scores']['放量突破'] = r.get('score', 0)
        for r in shrink_results:
            code = r['code']
            if code not in hit_map:
                hit_map[code] = {'name': r['name'], 'strategies': [], 'scores': {}}
            hit_map[code]['strategies'].append('缩量突破')
            hit_map[code]['scores']['缩量突破'] = r.get('score', 0)

        multi_hit = {k: v for k, v in hit_map.items() if len(v['strategies']) >= 2}

        # === 报告输出 ===
        print("\n")
        print("=" * 70)
        print("  A股多策略选股 - 筛选报告")
        print("=" * 70)

        # 总览
        print(f"\n总览（扫描 {total_scanned} 只，耗时 {elapsed:.1f}秒）")
        print("-" * 50)
        print(f"{'策略':<16} {'命中数':<10}")
        print("-" * 50)
        print(f"{'三日反转':<16} {len(reversal_results):<10}")
        print(f"{'放量突破':<16} {len(volume_results):<10}")
        print(f"{'缩量突破':<16} {len(shrink_results):<10}")
        total_hits = len(reversal_results) + len(volume_results) + len(shrink_results)
        print(f"{'合计（含重复）':<16} {total_hits:<10}")
        print(f"{'独立股票数':<16} {len(hit_map):<10}")
        print("-" * 50)

        # 多策略共振
        if multi_hit:
            print(f"\n多策略共振（同时被2+策略选中: {len(multi_hit)}只）")
            print("-" * 70)
            print(f"{'代码':<10} {'名称':<10} {'命中策略':<24} {'各策略评分':<20}")
            print("-" * 70)
            for code, info in sorted(multi_hit.items(), key=lambda x: len(x[1]['strategies']), reverse=True):
                strategies = ', '.join(info['strategies'])
                scores = ', '.join(f"{s}:{info['scores'][s]}" for s in info['strategies'])
                print(f"{code:<10} {info['name']:<10} {strategies:<24} {scores:<20}")
            print("-" * 70)
        else:
            print("\n多策略共振: 无（今日无股票被多个策略同时选中）")

        # 分策略详细结果
        # 策略1: 三日反转
        print(f"\n{'='*70}")
        print(f"策略一: 三日反转形态（命中 {len(reversal_results)} 只）")
        print(f"形态: 小阴线 + 大阴线 + 低开高收阳线")
        print(f"{'='*70}")
        if reversal_results:
            print(f"{'代码':<10} {'名称':<10} {'前置跌幅':<10} {'三日形态':<32} {'对比度':<8} {'上影线':<8} {'评分':<6}")
            print("-" * 90)
            for r in reversal_results:
                pattern_str = f"{r['day1_change']:+.1f}% > {r['day2_change']:+.1f}% > {r['day3_change']:+.1f}%"
                contrast = f"{r['contrast']:.1f}x"
                shadow = f"{r['shadow']:.0f}%"
                prior = f"{r['prior_decline']:+.1f}%"
                print(f"{r['code']:<10} {r['name']:<10} {prior:<10} {pattern_str:<32} {contrast:<8} {shadow:<8} {r['score']:<6}")
        else:
            print("今日无命中")

        # 策略2: 放量突破
        print(f"\n{'='*70}")
        print(f"策略二: 放量突破形态（命中 {len(volume_results)} 只）")
        print(f"形态: 前期下跌 > 止跌横盘 > 涨停板 > 回踩横盘 > 放量突破")
        print(f"{'='*70}")
        if volume_results:
            print(f"{'代码':<10} {'名称':<10} {'涨停日期':<12} {'涨停涨幅':<10} "
                  f"{'回踩天数':<10} {'回踩振幅':<10} {'突破量比':<10} {'评分':<6}")
            print("-" * 90)
            for r in volume_results:
                print(f"{r['code']:<10} {r['name']:<10} {r['limit_up_date']:<12} "
                      f"{r['limit_up_change']:>+.1f}%{'':>4} "
                      f"{r['post_consol_days']}天{'':>6} "
                      f"{r['post_range_pct']:.1f}%{'':>6} "
                      f"{r['break_vol_ratio']:.2f}x{'':>5} "
                      f"{r['score']:<6}")
        else:
            print("今日无命中")

        # 策略3: 缩量突破
        print(f"\n{'='*70}")
        print(f"策略三: 缩量突破形态（命中 {len(shrink_results)} 只）")
        print(f"形态: 下跌 > 横盘 > 涨停 > 横盘 > 缩量突破")
        print(f"{'='*70}")
        if shrink_results:
            print(f"{'代码':<10} {'名称':<10} {'前期跌幅':<10} {'横盘1天数':<10} "
                  f"{'涨停日期':<14} {'涨停形态':<10} {'横盘2天数':<10} {'MA5/MA10':<10} {'评分':<6}")
            print("-" * 100)
            for r in shrink_results:
                print(f"{r['code']:<10} {r['name']:<10} "
                      f"{'-' + str(r['decline_pct']) + '%':<10} "
                      f"{r['consolidation1_days']:<10} "
                      f"{r['limit_up_date']:<14} "
                      f"{r['limit_up_type']:<10} "
                      f"{r['post_days']:<10} "
                      f"{r['vol_ratio']:<10} "
                      f"{r['score']:<6}")
        else:
            print("今日无命中")

        # 综合推荐 Top 5
        print(f"\n{'='*70}")
        print("综合推荐 Top 5")
        print(f"{'='*70}")
        all_stocks = []
        for r in reversal_results:
            all_stocks.append({
                'code': r['code'], 'name': r['name'],
                'strategy': '三日反转', 'score': r.get('score', 0),
                'multi_hit': r['code'] in multi_hit,
                'hit_count': len(hit_map.get(r['code'], {}).get('strategies', [])),
            })
        for r in volume_results:
            all_stocks.append({
                'code': r['code'], 'name': r['name'],
                'strategy': '放量突破', 'score': r.get('score', 0),
                'multi_hit': r['code'] in multi_hit,
                'hit_count': len(hit_map.get(r['code'], {}).get('strategies', [])),
            })
        for r in shrink_results:
            all_stocks.append({
                'code': r['code'], 'name': r['name'],
                'strategy': '缩量突破', 'score': r.get('score', 0),
                'multi_hit': r['code'] in multi_hit,
                'hit_count': len(hit_map.get(r['code'], {}).get('strategies', [])),
            })

        # 去重（同一股票取最高分记录），多策略共振优先
        seen = {}
        for s in all_stocks:
            code = s['code']
            if code not in seen or s['score'] > seen[code]['score']:
                seen[code] = s

        ranked = sorted(seen.values(), key=lambda x: (x['hit_count'], x['score']), reverse=True)

        if ranked:
            for i, s in enumerate(ranked[:5], 1):
                multi_tag = " [多策略共振]" if s['multi_hit'] else ""
                strategies = ', '.join(hit_map[s['code']]['strategies'])
                print(f"  {i}. {s['code']} {s['name']} - {strategies} (最高评分: {s['score']}){multi_tag}")
        else:
            print("  今日无推荐")

        # CSV文件路径
        print(f"\n{'='*70}")
        print("结果文件")
        print(f"{'='*70}")
        if csv_files.get('reversal'):
            print(f"  三日反转: {csv_files['reversal']}")
        if csv_files.get('volume'):
            print(f"  放量突破: {csv_files['volume']}")
        if csv_files.get('shrink'):
            print(f"  缩量突破: {csv_files['shrink']}")
        if not csv_files:
            print("  今日无命中，未生成CSV文件")

        # 风险提示
        print(f"\n{'='*70}")
        print("风险提示: 本工具仅供学习研究使用，不构成任何投资建议。股市有风险，投资需谨慎。")
        print(f"{'='*70}")


def main():
    screener = CombinedScreener()
    screener.run()


if __name__ == "__main__":
    main()
