import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

export class ErrorBoundary extends Component<{
  children: ReactNode;
  fallback: ReactNode;
  resetKey?: string;
}, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    console.error("界面渲染失败", error.name);
  }

  componentDidUpdate(previous: Readonly<{ resetKey?: string }>) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey)
      this.setState({ failed: false });
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export function PageRecovery() {
  return <main className="welcome" role="alert">
    <h1>这页暂时无法显示</h1>
    <p>聊天数据仍然保留，可以先返回首页再选择其他会话。</p>
    <button className="primary-button" onClick={() => {
      localStorage.removeItem("travel-active");
      window.location.reload();
    }}>返回首页</button>
  </main>;
}
