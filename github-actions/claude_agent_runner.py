"""
Claude Agent SDK 执行器（tool_use 模式）
使用 Anthropic Python SDK 的 tool_use 功能实现真正的 Agent 循环：
1. Claude 调用 run_stock_screener → 执行筛选脚本
2. Claude 调用 read_screening_results → 读取结果数据
3. Claude 生成分析报告 → 保存到文件
"""

import anthropic
import subprocess
import sys
import os
import json
from datetime import datetime


# 定义 Tools
TOOLS = [
    {
        "name": "run_stock_screener",
        "description": "运行三合一A股选股器，执行三日反转/放量突破/缩量突破三个策略筛选全市场股票。返回筛选过程的控制台输出摘要。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "read_screening_results",
        "description": "读取筛选结果JSON文件，获取各策略命中的股票列表及详细数据。包含三日反转、放量突破、缩量突破三个策略的完整筛选结果和评分。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
]

SYSTEM_PROMPT = """你是一位专业的A股技术分析师，擅长通过K线形态识别潜在的交易机会。

你有两个工具可以使用：
1. run_stock_screener - 运行三合一选股器，筛选全市场股票
2. read_screening_results - 读取筛选结果数据

请按以下步骤执行：
1. 先调用 run_stock_screener 运行筛选
2. 再调用 read_screening_results 获取详细结果
3. 基于数据生成专业的分析报告

分析报告应包含：
1. **筛选总览** - 各策略命中数量统计
2. **多策略共振** - 被2+策略同时命中的股票（重点分析，可信度更高）
3. **各策略精选分析** - 每个策略评分最高的前3只股票的技术分析
4. **综合推荐 Top 5** - 综合评分和共振情况给出优先关注列表
5. **风险提示** - 当前市场环境下需注意的风险因素

注意：本分析仅供学习研究使用，不构成任何投资建议。"""


def execute_tool(tool_name, tool_input):
    """执行工具调用"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, 'output')

    if tool_name == "run_stock_screener":
        screener_script = os.path.join(script_dir, 'combined_screener_cloud.py')
        print("  正在执行筛选脚本...")
        result = subprocess.run(
            [sys.executable, screener_script],
            capture_output=True,
            text=True,
            env={**os.environ, 'OUTPUT_DIR': output_dir},
            timeout=3600  # 串行遍历可能需要较长时间
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]: {result.stderr}"
        # 截取最后部分避免输出过长
        if len(output) > 10000:
            output = output[:2000] + "\n...[中间省略]...\n" + output[-8000:]
        return output

    elif tool_name == "read_screening_results":
        results_file = os.path.join(output_dir, 'latest_results.json')
        if not os.path.exists(results_file):
            return json.dumps({"error": "筛选结果文件不存在，请先运行 run_stock_screener"})
        with open(results_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        result_json = json.dumps(data, ensure_ascii=False, indent=2)
        # 防止超长
        if len(result_json) > 50000:
            result_json = result_json[:50000] + "\n...[数据截断]"
        return result_json

    return json.dumps({"error": f"未知工具: {tool_name}"})


def save_report(analysis_text):
    """保存分析报告"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(script_dir, 'output')
    os.makedirs(output_dir, exist_ok=True)

    # 保存 Markdown 报告
    report_path = os.path.join(output_dir, 'ai_analysis_report.md')
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"# A股三合一多策略选股 AI 分析报告\n\n")
        f.write(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(analysis_text)
        f.write(f"\n\n---\n*本报告由 Claude AI 自动生成，仅供学习研究，不构成投资建议。*\n")
    print(f"\n分析报告已保存到: {report_path}")

    # 追加到 GitHub Actions Summary
    github_step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if github_step_summary:
        with open(github_step_summary, 'a', encoding='utf-8') as f:
            f.write(f"\n\n## AI 分析报告\n\n{analysis_text}\n")
        print("已追加 AI 分析到 GitHub Actions Summary")


def run_agent():
    """运行 Claude Agent 循环（tool_use 模式）"""

    # 检查 API Key
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print("错误: 未设置 ANTHROPIC_API_KEY 环境变量")
        sys.exit(1)

    # 支持自定义 API Base URL（用于第三方代理服务）
    base_url = os.environ.get('ANTHROPIC_BASE_URL')
    client_kwargs = {'api_key': api_key}
    if base_url:
        client_kwargs['base_url'] = base_url

    client = anthropic.Anthropic(**client_kwargs)

    # 支持通过环境变量切换模型（空字符串也用默认值）
    model = os.environ.get('CLAUDE_MODEL') or 'claude-sonnet-4-5-20250929'

    print("=" * 60)
    print("Claude Agent SDK 执行器 (tool_use 模式)")
    print(f"模型: {model}")
    print("=" * 60)

    # 初始消息
    messages = [
        {
            "role": "user",
            "content": "请执行A股三合一多策略选股筛选，分析结果并生成专业报告。"
        }
    ]

    # Agent 循环
    max_iterations = 10
    for iteration in range(max_iterations):
        print(f"\n--- Agent 迭代 {iteration + 1} ---")

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        print(f"stop_reason: {response.stop_reason}")

        # Agent 完成，提取最终文本
        if response.stop_reason == "end_turn":
            final_text = ""
            for block in response.content:
                if block.type == "text":
                    final_text += block.text

            print("\n" + "=" * 60)
            print("Claude AI 分析报告")
            print("=" * 60)
            print(final_text)

            # 保存报告
            save_report(final_text)
            break

        # Agent 请求调用工具
        elif response.stop_reason == "tool_use":
            # 将助手消息加入对话历史
            messages.append({"role": "assistant", "content": response.content})

            # 处理每个工具调用
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"  调用工具: {block.name}")
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
                    print(f"  工具返回: {len(result)} 字符")

            # 将工具结果加入对话历史
            messages.append({"role": "user", "content": tool_results})

        else:
            print(f"未知的 stop_reason: {response.stop_reason}")
            break
    else:
        print("警告: 达到最大迭代次数限制")


if __name__ == "__main__":
    run_agent()
