# Task Logging

## install

``` bash
poetry add task-logging@git+https://gitlab.zhong-server.com/social-robot/common/task-logging.git@main
```

## 选型

1. 首先使用同步模型完成编写，0.1.0版本，主要就是简单
2. 数据库使用sqlalchemy，pgsql
3. 继承python自带的loggger, 这样所有写了log的服务全都不用修改，只需要替换一下log的定义就行了，就非常简单
4. 首先保存日志需要一张表，这张表需要表定义，我们需要设计一下

## 日志表定义

1. 主键应该是一个stirng，其实本来我们是想用redis的list的，后来考虑到日志的长期存储问题，以及大量日志会占用大量内存的问题，选择了pg。
2. pg就是比redis会慢一些。不过在这个项目里面，速度并不是关键。所以就选择了pg
3. 因为pg没有redis那样的expire，所以需要一个时间戳，来记录某个key的时间，因为某一个任务的执行时间顶多几分钟，所以就采取这个task的创建时间就ok了。
4. 还有就是，这个主键没有必要和task-status表有约束，虽然我们想的是她的主键就是taskid，但是也有可能不是，为了最大的灵活性
5. 然后这张表是需要真删除的，所以不需要有is_delete这个字段
6. 每个单独的服务都有自己的一个单独的数据库表，
7. 日志等级
8. 每条日志占数据库中的一行？主键是自增id
9. 然后使用一个taskid作为外建？不用外建最好的，就抓门有一个taskid字段，这样就可以根据taskid来查询日志了，每条日志都有时间戳，这个也比较合理
10. 日志的内容，就是一个text字段就行了

11. service_name是合理的，就算是在一个应用中，也有可能有不同的模块，这个时候这个东西就有用了。

```deepseek
根据需求讨论和最佳实践，以下是推荐的日志表设计：

### **表名**
建议统一使用单一表结构，通过`service_name`字段区分不同服务（而非分表），提高管理效率。若严格要求每个服务独立表，可在表名中加入服务标识（如`{service_name}_task_logs`），但需处理动态建模的复杂性。

---

### **表结构定义（共用表方案）**
```sql
CREATE TABLE task_logs (
    task_id VARCHAR(36) PRIMARY KEY,         -- 主键，全局唯一任务ID
    service_name VARCHAR(100) NOT NULL,      -- 服务名称标识
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 任务创建时间
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 最后更新时间
    log_level VARCHAR(20),                   -- 最后一条日志的级别（INFO/WARNING/ERROR等）
    log_content TEXT NOT NULL                -- 聚合后的完整日志文本
);

-- 索引建议
CREATE INDEX idx_task_logs_service ON task_logs (service_name);
CREATE INDEX idx_task_logs_created ON task_logs (created_at);
```

---

### **关键设计解析**

1. **主键设计**
   - 使用`task_id`作为主键（VARCHAR类型），直接标识任务，避免关联其他表约束。
   - 若需支持同一任务多日志条目（更佳实践），建议改用自增主键+`task_id`外键（见附录）。

2. **日志存储方式**
   - `log_content`为**追加式文本**，每次更新时追加新日志行（需应用层控制并发写入）。
   - 简单但牺牲细粒度查询，适合0.1.0版本的快速实现。

3. **时间字段**
   - `created_at`标记任务创建时间，用于清理过期日志（如定期删除3天前的日志）。
   - `updated_at`自动更新，追踪最后日志时间。

4. **服务区分**
   - `service_name`字段标识来源服务，避免分表带来的维护成本。

5. **扩展性**
   - 可添加`metadata JSONB`字段存储额外上下文（如用户ID、主机IP等），增强灵活性。

---

### **附录：事件分拆表（推荐进阶方案）**

若需支持每条日志独立存储（如按时间/级别过滤），推荐以下设计：

```sql
CREATE TABLE task_log_events (
    id BIGSERIAL PRIMARY KEY,                -- 自增主键
    task_id VARCHAR(36) NOT NULL,            -- 关联任务ID
    service_name VARCHAR(100) NOT NULL,      -- 服务名称
    log_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 日志生成时间
    log_level VARCHAR(20) NOT NULL,          -- 日志级别
    message TEXT NOT NULL                    -- 原始日志内容
);

-- 索引建议
CREATE INDEX idx_task_events_task ON task_log_events (task_id);
CREATE INDEX idx_task_events_service_time ON task_log_events (service_name, log_time);
```

---

### **选型建议**

- **简单场景**：使用**共用表方案**，快速实现日志聚合，牺牲部分灵活性。
- **生产环境**：采用**事件分拆表**，符合日志审计和分析需求，扩展性更强。
- **数据清理**：通过`created_at`或`log_time`字段配置定时任务（如Celery Beat）删除旧数据。

```

感觉第二种更加合理一些


```qwen


好的，我将详细解释为什么在原始表设计中需要添加以下新字段（这些字段在原始表定义中不存在）：

---

### **1. `hostname VARCHAR(255)`**
#### **为什么添加？**
- **分布式系统定位**：在分布式环境中，同一服务可能在多个主机上运行。记录日志产生的主机名（如服务器IP或主机名）可以快速定位问题来源。
- **故障隔离**：如果某个主机频繁出现错误，可以通过该字段快速识别问题是否与特定主机的配置、资源或网络有关。
- **负载分析**：统计各主机的日志量分布，帮助优化资源分配。

#### **示例用例**：
```sql
SELECT hostname, COUNT(*)
FROM task_log_events
WHERE log_level = 'ERROR'
GROUP BY hostname;
-- 可以快速发现某台主机是否异常
```

---

### **2. `process_id INT`**

#### **为什么添加？**

- **进程级追踪**：同一主机可能运行多个进程。记录进程ID（PID）可区分不同进程的日志，便于排查进程级别的资源泄漏或崩溃。
- **容器/微服务场景**：在容器化部署中，每个容器可能有独立的PID空间，该字段有助于关联容器实例与日志。

#### **示例用例**

```sql
SELECT process_id, log_level, message
FROM task_log_events
WHERE task_id = 'specific-task-id'
ORDER BY log_time;
-- 追踪特定任务在特定进程中的执行过程
```

---

### **3. `thread_name VARCHAR(255)`**

#### **为什么添加？**

- **多线程调试**：在多线程应用中，记录线程名称（如`main-thread`或`worker-123`）可快速定位线程级别的死锁、资源竞争或任务执行路径。
- **异步任务追踪**：在异步框架（如Celery）中，线程名可关联到具体的任务队列或工作流。

#### **示例用例**

```sql
SELECT thread_name, COUNT(*)
FROM task_log_events
WHERE log_level = 'WARNING'
AND service_name = 'payment-service'
GROUP BY thread_name;
-- 检查特定线程是否频繁出现警告
```

---

### **4. `exception_details TEXT`**

#### **为什么添加？**

- **错误堆栈隔离**：原始的`message`字段可能包含混合信息（如错误描述+堆栈），单独存储堆栈信息可提升查询效率。
- **错误类型分析**：可以快速统计特定异常类型（如`ConnectionError`或`TimeoutError`）的发生频率，而无需解析整个日志内容。
- **调试友好性**：直接通过`exception_details`字段搜索特定异常类型，避免噪声干扰。

#### **示例用例**

```sql
SELECT exception_details
FROM task_log_events
WHERE log_level = 'ERROR'
AND exception_details LIKE '%Connection refused%'
LIMIT 10;
-- 快速定位网络连接问题
```

---

### **5. `metadata JSONB`**

#### **为什么添加？**

- **结构化日志支持**：Python应用通常需要记录结构化数据（如用户ID、请求ID、响应时间等），JSONB允许灵活存储这些元数据而不修改表结构。
- **查询扩展性**：通过JSONB的索引（如GIN索引）可以高效查询嵌套字段，例如：
  - `WHERE metadata->>'user_id' = '123'`
  - `WHERE (metadata->'http'->>'status')::int >= 500`
- **多服务兼容性**：不同服务可以记录不同字段，而无需统一表结构。

#### **示例用例**

```sql
SELECT *
FROM task_log_events
WHERE metadata @> '{"http": {"status": 500}}';
-- 查询所有HTTP 500错误
```

---

### **关键改进点总结**

| 字段 | 解决的问题 | 典型用例 |
|------|------------|----------|
| `hostname` | 定位问题主机 | 快速识别故障节点 |
| `process_id` | 进程级调试 | 分析进程资源使用 |
| `thread_name` | 线程级追踪 | 排查线程死锁 |
| `exception_details` | 异常类型分析 | 统计特定错误频率 |
| `metadata` | 结构化数据查询 | 按用户或请求ID过滤 |

---

### **补充说明**

1. **性能与存储权衡**：
   - JSONB字段在查询时需要解析，但其灵活性远大于存储成本，且通过索引可优化查询速度。
   - 对于写入密集型场景，可将高频字段（如`user_id`）拆分为单独列，低频字段用JSONB。

2. **兼容性**：
   - 如果现有系统已依赖原始表结构，可以通过`ALTER TABLE`逐步添加字段，避免中断服务。

3. **未来扩展**：
   - 新增字段为后续功能（如日志分析仪表盘、智能告警）提供了数据基础，避免因需求变更频繁修改表结构。

---

通过这些新增字段，日志表不仅能记录基础信息，还能支持更复杂的分析、调试和监控需求，符合生产环境下的日志管理最佳实践。

## 我的设计

1. 感觉上面说的都挺合理的，那么结合他们的设计，已经咱们的系统，具体问题，具体分析
2. 可以这样设计，因为咱们的服务都是跑在容器里面的，所以记录hostname，ip等的信息意义不大，
3. 但是出了记录日志，还记录这个异常栈这个还是挺有用的，
4. hostname，ip，process，thread可以记录，反正也无伤大雅，万一有用呢
5. meatadata这个字段跟AI继续来看，他是想继续保存task的相关信息，但是这个可以用另外一张表来保存，比如task_info, 这样就不用在这个表里面保存了。
6. ok，那么我们就这样设计吧
7. 异常还是比较关键的，当log lelve是error的时候，我们就应该记录日志信息，同时记录异常的名字，这样可以根据异常的名字来搜索。
8. 如果你想要多表联合查询，这些表需要在同一个数据库里面啊，所以整个服务的数据库还是只有一个是比较合理的。
9. 不同的业务确实可以分表，但是一般同一个业务的日志还是放在一张表里面比较好，这样可以方便查询。
10. service_name表示的是业务内部的某个服务的名字，这个名字是唯一的，这个名字是可以在业务内部唯一的标识一个服务的。

```sql
CREATE TABLE task_log_events (
    id BIGSERIAL PRIMARY KEY,                -- 自增主键
    task_id VARCHAR(36) NOT NULL,            -- 关联任务ID
    service_name VARCHAR(100) NOT NULL,      -- 服务名称
    log_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 日志生成时间
    log_level VARCHAR(20) NOT NULL,          -- 日志级别
    message TEXT NOT NULL,                   -- 原始日志内容
    hostname VARCHAR(255),                   -- 主机名
    process_id INT,                          -- 进程ID
    thread_name VARCHAR(255),                -- 线程名
    exception_name VARCHAR(100),             -- 异常类型
    exception_details TEXT,                  -- 异常堆栈
    -- metadata JSONB                          -- 元数据
);
```

## 一些思考导致的架构设计

1. 因为engine连接的是整个数据库，如果不同的业务使用不同的数据库，那么对他们而言，这个包将无法使用，因为表的名字是固定的，没有一个相对简单的方法，每个业务只需要引入这个包，就可以简单的创建一个新的表，使用这个包。所以service_name是必须的，而且他不能用来表示业务内部的某个模块的名字，他就是用来表示不同的业务的！
2. 如果在业务内部，还想要区分不同的模块，那么只能再网上加键，比如 module_name, function_name等。
3. 这样整个系统公用这一张表，就会让编码变得简单一些！
4. 因为orm给我们封装了底层数据库，所以这里的测试就使用sqlite就行了，不用担心数据库的问题
5. 这样这个包也不需要用docker来开发，就更简单了。

在 Python 中，当异常触发时，可以通过以下方法保存当时的现场环境（如变量状态、堆栈信息等），以便后续调试或分析：

---

### **方法 1：使用 `try-except` 捕获异常并记录信息**

在 `except` 块中，通过 `sys.exc_info()` 和 `traceback` 模块获取异常详细信息，并结合 `inspect` 模块捕获当前变量状态。

```python
import sys
import traceback
import inspect

def save_environment():
    # 获取当前帧（异常发生时的环境）
    frame = inspect.currentframe()
    # 提取局部变量和全局变量
    locals_data = frame.f_back.f_locals  # 局部变量
    globals_data = frame.f_back.f_globals  # 全局变量
    # 保存或记录这些变量（例如保存到文件或日志）
    with open("error_context.txt", "w") as f:
        f.write(f"Locals: {locals_data}\n")
        f.write(f"Globals: {globals_data}\n")

try:
    # 你的代码（示例：触发一个异常）
    x = 1 / 0
except:
    # 记录异常信息和堆栈跟踪
    exc_type, exc_value, exc_traceback = sys.exc_info()
    error_message = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    # 保存现场环境
    save_environment()

    # 输出或记录错误信息
    with open("error_log.txt", "w") as f:
        f.write(f"Exception occurred:\n{error_message}\n")
    raise  # 可选：重新抛出异常
```

---

### **方法 2：自定义全局异常钩子（`sys.excepthook`）**

覆盖 Python 的默认异常处理逻辑，在程序崩溃时自动保存环境。

```python
import sys
import traceback
import inspect

def custom_excepthook(exc_type, exc_value, exc_traceback):
    # 获取异常发生时的堆栈帧
    frame = exc_traceback.tb_frame
    locals_data = frame.f_locals
    globals_data = frame.f_globals

    # 保存环境信息
    with open("crash_context.txt", "w") as f:
        f.write(f"Exception Type: {exc_type}\n")
        f.write(f"Exception Message: {exc_value}\n")
        f.write("Traceback:\n")
        f.write("".join(traceback.format_tb(exc_traceback)))
        f.write(f"\nLocals: {locals_data}\n")
        f.write(f"Globals: {globals_data}\n")

    # 调用默认异常处理（可选）
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

# 设置全局异常钩子
sys.excepthook = custom_excepthook

# 触发异常的代码示例
x = 1 / 0
```

---

### **方法 3：使用 `logging` 模块记录完整信息**

结合 `logging` 模块和 `traceback`，将错误信息和环境状态记录到日志文件。

```python
import logging
import sys
import inspect

logging.basicConfig(filename='app.log', level=logging.ERROR)

try:
    x = 1 / 0
except Exception as e:
    # 获取当前帧的局部变量
    frame = inspect.currentframe().f_back
    locals_data = frame.f_locals

    # 记录错误和变量状态
    logging.error(f"Exception: {str(e)}")
    logging.error(f"Local variables: {locals_data}")
    logging.error("Traceback:", exc_info=True)
```

---

### **方法 4：使用调试器保存现场（如 `pdb`）**

在异常触发时自动启动交互式调试器，直接查看现场环境。

```python
import pdb

try:
    x = 1 / 0
except:
    pdb.post_mortem()  # 启动调试器查看当前状态
```

---

### **关键点总结**

1. **捕获变量状态**：使用 `inspect` 模块的 `f_locals` 和 `f_globals` 获取局部/全局变量。
2. **记录堆栈跟踪**：使用 `traceback.format_exc()` 或 `sys.exc_info()` 获取完整的错误堆栈。
3. **持久化存储**：将信息保存到文件（如 `.txt` 或日志）或发送到远程服务。
4. **全局处理**：通过 `sys.excepthook` 捕获未处理的异常。

根据需求选择合适的方法，调试时推荐使用 `pdb`，生产环境建议结合日志记录。

异常的自动保存现场，然后添加到日志中是非常不多的
但是用钩子是不合适的，因为在钩子里面，我们没有办法知道taskID
所以比较合适的处理方式就是在我们捕获一个异常的时候，手动的保存现场，然后添加到日志中去
我们可以提供一个简单的函数，来帮助做这件事情
包括自动获得异常的名字 异常的信息 异常的栈 等等 全都是自动获取的话 就非常非常方便了

## TIPS

1. 为了最大的简化操作，所有能够自动获取的信息，才能放到表里！
2. globals和locals太多了，不适合放到表里面，所以只能放到文件里面去！一般情况下也没有必要
3. ok！设计终于都敲定了！
4. 休息吧，明天上午把log给写好，下午把webdriver给写好。然后把现在的common里面的东西都整理整理。周末休息一天。找振宇交接一下。

## TODO

1. 现在整个库的基础功能都实现完了，0.1.0版本涉及的功能都实现完了
2. 增加cicd precommit那一套
3. 因为这个库的实现有很多的trick！需要好好的注释，然后加强单元测试！
