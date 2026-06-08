# TODO

1. [x] 对照我之前的代码，把现在这份代码读懂
2. [x] 为什么必须用json格式的日志呢？
3. [x] 把grafana那一堆部署起来，尝试启动两个或者多个log service，看看日志都是怎么读取和显示的，可以把截图放到readme里面
4. [x] https://docs.python.org/3/howto/logging-cookbook.html#adding-contextual-information-to-your-logging-output 这个就是我们要找的解决方案，最终用的是contextvar 有依据可循就行，看看 https://docs.python.org/3/howto/logging-cookbook.html#use-of-contextvars
5. [x] https://docs.python.org/3/howto/logging-cookbook.html#customizing-logrecord 这个可能也也用，在把log record转成json的时候
6. [x] https://docs.python.org/3/howto/logging-cookbook.html#customized-exception-formatting
7. [x] 还差一个custom formatter的教程
8. [x] 我还要看一下之前是怎么收集我们要输出的日志内容的 https://docs.python.org/3/library/logging.html#formatter-objects 因为实在找不到教程，所以找到了reference，其实就是重载一下format这个函数就行了
9. [x] https://docs.python.org/3/library/logging.html#logrecord-objects 这个也是有用的，在formatter里面
10. [x] 现在使用loki grafana，日志都是json的，那么去拿stack depth已经没有意义了，不过还是拿上吧，我可以写一个小工具来格式化这一部分json自动排版，可以生成一个网页
11. [x] 我们第一版还有两个点有特殊的处理
    1.  [x] 异常信息，stack trace，locals
    2.  [x] 在log decorator的时候，是有ENTER和EXIT的，还会统计运行时间
    3.  [x] 拿函数名字的时候是通过栈帧拿的，而不是LogRecord，这个需要测试一下看看拿的对不对
    4.  [x] 把原来的仓库的测试案例都拿过来继续测试
    5.  [x] 不对，我确实用不到stack depth，我用 ENTER xxx 和 EXIT xxx就可以进行配对了
