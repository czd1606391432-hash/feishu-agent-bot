"""测试用的 MCP 服务（stdio）：两个小工具。只在装了 mcp SDK 时由 test_with_sdk.py 拉起。"""

try:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP
except ImportError:      # mcp 2.x 改名了
    from mcp.server.mcpserver import MCPServer as FastMCP

server = FastMCP("demo")


@server.tool(name="math.add")
def add(a: int, b: int) -> str:
    """两数相加"""
    return str(a + b)


@server.tool(name="text.repeat")
def repeat(text: str, times: int = 2) -> str:
    """把一段文字重复几遍"""
    return text * times


if __name__ == "__main__":
    server.run()
