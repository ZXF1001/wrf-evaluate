# WRF 预报检验

按统一配置，将 WRF 最近网格点与 CLDAS、区域站实测进行比较。运行时只读原始 WRF、NetCDF 和 Excel 文件；不会改写、转换或缓存输入。图中各来源叠加在同一坐标轴：WRF 降水绘为逐小时雨量，其他曲线保留原生时间分辨率；指标独立配对。每次运行在 `output/<name>_<时间>/` 创建结果目录，保存 PNG、CSV 和当次 `config.toml` 副本。

当前 `plot` 支持 2 米气温、2 米相对湿度、10 米风速、降水、地表向下短波辐射和长波辐射。EC、国家站读取暂未接入。起报目录名按来源时区解释；平铺文件回退读取 WRF 的 `SIMULATION_START_DATE` 或 `START_DATE`，也可用 `init_time` 明确覆盖。

`plot` 还会按 `[maps].interval`（默认 `6h`）从 `period.start` 到 `period.end` 尝试输出 WRF/CLDAS 并排空间填色图，使用 `elements` 中的全部要素。空间图只在双方具有严格对应时刻或完整时段时生成，不插值、不重网格；缺少对应场或时段会在运行进度中说明并跳过。

空间图左右使用相同经纬度范围、单位和色标，范围取 WRF 网格的经纬度外接矩形。CLDAS 只读取该矩形内的子区域；两者保留原生网格，不输出格点差值或空间指标。目前图上没有行政边界底图。降水/辐射的比较窗口由 CLDAS 映射的 `interval`、`timestamp` 定义，**出图间隔不是雨量累计时长**。例如每 6 小时出一张此前 1 小时的雨量图。窗口超出 `period` 时跳过；WRF 累计量逐次检查差分，清零或时段内网格移动会报错。

```toml
[maps]
interval = "6h" # 可省略，默认六小时；也支持 30min、1h 等
```

运行会即时打印当前阶段，日志前缀为输出时的时间，格式为 `[YYYY-MM-DD HH:MM:SS]`，时区沿用 `[output].timezone`。WRF 时间索引供点位与空间图复用，CLDAS 点位取值按文件一次提取所有点位和所需变量；Excel 先筛站号、时段再解析气象数值。原始数据仍只读，无持久数据缓存。较大的 Excel 初次加载及单次底层文件读取期间只能显示当前阶段，不能报告字节级进度。

绘图前日志会显示各条曲线的首末有效时次、有效点数及开头未覆盖的时长（时段量按首个有效区间的起点计算），时间使用输出时区。WRF 起报日志同时显示该批次加上最低时效后的允许使用起点。横轴始终使用 `period`：若缺少能覆盖起点的合格批次或有效数值，开头保留空白，不平移曲线、不填补缺测。

WRF 点位定位在同一来源的多个文件间复用，只在内存保留上一帧网格和定位结果。每帧仍精确核对经纬度及缺测掩码；网格变化时重新定位，重复经纬度只定位一次，不依赖文件名推断网格固定。

空间图连续读取同一 WRF 文件时复用文件句柄，同一帧中重复使用的 T2、经纬度等只读一次。换文件立即关闭旧文件，换帧释放旧场，绘图结束或报错也会关闭文件；只在内存保留当前帧按需读取的字段，不生成数据缓存文件。出图计划只保留符合 `maps.interval` 的时刻。

## Linux 运行

需要 Python 3.11+ 和 `uv`。依赖安装到项目 `.venv`，不使用系统 Python 环境：

```bash
uv sync --locked
```

编辑 `config.toml` 中的 Linux 数据路径、时间范围、点位、要素和字段映射，然后运行：

```bash
.venv/bin/python -m wrf_evaluate --config config.toml check
.venv/bin/python -m wrf_evaluate --config config.toml plot
```

`check` 校验配置和路径，不读取气象数据、不建输出目录。Linux 无桌面环境可绘图；图中文字需要系统有中文字体，例如 Noto Sans CJK SC。

## 变量映射

配置左侧统一使用程序要素名，右侧 `variable` 是原始文件中的一个变量名或 Excel 表头。每个要素通常映射一个变量；WRF 的 `wind_speed_10m` 默认由同一质量网格上的 `U10`、`V10` 合成，也可配置 `components = ["U10", "V10"]`。相对湿度可由 `inputs` 中指定的水汽量、温度、气压计算，详见下节。降水仍只读取所指定的一个累计或时段字段，不会自动把 `RAINC`、`RAINNC` 或 `RAINSH` 相加。单位和时间含义也在同一要素子表中配置。

样例 WRF 属性明确标注：`RAINC` 为累计积云降水，`RAINNC` 为累计网格尺度降水，`RAINSH` 为累计浅积云降水，均为 mm。这能确认 WRF 的累计含义，但不能用来解释站点 Excel 的降水列。

WRF 的 `T2`、`U10/V10`、上述降水和地面辐射场已在质量网格上，无需去交错。风速按 `hypot(U10, V10)` 计算；水平坐标旋转不改变风速大小。三维 `U/V/W` 的交错网格、高空层选择及风向旋转尚未接入，误映射这些变量会报错，不能把去交错后的最低层风当成 10 米风。累计量也不允许跨起报或跨移动格点相减。

| 要素名 | 统一单位 | 支持的时间含义 |
| --- | --- | --- |
| `temperature_2m` | ℃ | `instant`、`mean` |
| `relative_humidity_2m` | % | `instant`、`mean` |
| `wind_speed_10m` | m/s | `instant`、`mean` |
| `precipitation` | mm/时段 | `sum`、`cumulative`；区域站 `raw` 仅绘图 |
| `surface_shortwave_down` | W/m² | `instant`、`mean`、辐射能量 `sum`/`cumulative` |
| `surface_longwave_down` | W/m² | `instant`、`mean`、辐射能量 `sum`/`cumulative` |

`instant` 是输出时刻的值；`mean` 是指定时长的平均值；`sum` 是指定时段内的量；`cumulative` 是不断累加的读数，程序对相邻时次作差。`mean` 与 `sum` 必须设置 `interval` 和时间戳含义 `timestamp = "start"` 或 `"end"`。累积量首次可计算的差分需要前一个读数；检验范围外的区间会被排除。缺测不跨时次填补，负累计差分会报错，不自动猜测模式清零或重启规则。

示例：

```toml
elements = ["temperature_2m", "precipitation"]

[sources.wrf.variables.temperature_2m]
variable = "T2"
# WRF 原单位从文件 units 读取；K 会转为 ℃。

[sources.wrf.variables.precipitation]
variable = "RAINC"
units = "mm"
time_kind = "cumulative"

[sources.wrf.variables.surface_shortwave_down]
variable = "SWDOWN"
units = "W/m2"
time_kind = "instant"

[sources.cldas.variables.temperature_2m]
product = "TMP"  # CLDAS 文件名产品代码
variable = "TAIR" # NetCDF 内的字段名

[sources.cldas.variables.precipitation]
product = "PRE"
variable = "PRCP"
units = "mm/h"
time_kind = "sum"
interval = "1h"
timestamp = "end"

[sources.regional.variables.temperature_2m]
variable = "气温(°)" # Excel 表头
units = "degC"
time_kind = "instant"

[sources.regional.variables.precipitation]
variable = "小时降水量(mm)"
units = "mm"
time_kind = "raw" # 默认值；按原值绘图，不推断时间窗口，不计算该来源降水指标
```

将需要的示例表复制到对应来源配置中。`config.toml` 启用气温和相对湿度；未列入 `elements` 的映射不会进入本次读取。CLDAS 的产品代码和变量名必须分别配置。相对湿度不接受比湿直接映射；WRF 10 米风速默认由 `U10/V10` 合成，不能把单个风分量写成风速。Excel 没有可靠的单位元数据，所选每个实测变量都要在配置中填写 `units`。默认映射只覆盖样例中已经核对的字段；其余请明确指定。

区域站降水默认为原始时段雨量：加入 `precipitation` 后会按 Excel 记录时刻和原值绘图，不差分、不假设窗口，也不参与站点降水指标。WRF 累计场先在单一起报内逐时次差分，再合成为不重叠的完整逐小时雨量；不足一小时、有数据缺口或跨越起报边界的区间不会冒充小时雨量。若 CLDAS 降水也配置为相同的一小时窗口，WRF—CLDAS 可照常计算指标。只有在确认站点降水区间定义后，才能将站点映射明确配置为 `sum` 并设置 `interval`、`timestamp`，启用站点降水指标。辐射示例只有 WRF 映射，实际加入 `elements` 时还须配置具有相同时间含义的实况来源。

WRF 点位雨量的图和指标使用同一份整点逐小时序列。完整小时必须落在 `period` 和有效预报时效内；例如起点为 08:00，第一段是 08:00–09:00，绘在 09:00。分钟累计读数可以合成小时量，小时量不会拆成分钟量。区域站 `raw` 只影响该来源的降水指标，不影响它的气温、湿度或风速检验。若只有 `raw` 降水实况，仍输出曲线和仅含表头的 CSV，并在日志说明没有计算站点降水指标。

降水单位 `mm/h` 只在已明确配置的 `sum` 时段中，按该时段平均雨强乘时长换成 mm；瞬时雨强不能由此推算时段雨量。辐射能量和通量也不会只改单位标签而不换算。

辐射若原始变量是时段能量（J/m²），配置为 `sum` 或 `cumulative`，程序会按真实时段秒数换成平均通量 W/m²；若原始数据已经是通量则使用 `instant` 或 `mean`，并按文件时次/配置窗口绘图和检验。

## 相对湿度

将 `relative_humidity_2m` 加入 `elements`，即可使用已核对的默认输入：WRF 使用 `Q2/T2/PSFC`，CLDAS 使用 `SHU/QAIR`、`TMP/TAIR`、`PRS/PAIR`。站点仍直接读取映射的相对湿度列。湿度结果同时用于曲线、RMSE/MAE/Bias/R 和空间图；无需新增命令。

需要更换字段名或覆盖单位时，在统一配置中填写：

```toml
[sources.wrf.variables.relative_humidity_2m.inputs]
mixing_ratio = { variable = "Q2" }
temperature = { variable = "T2" }
pressure = { variable = "PSFC" }

[sources.cldas.variables.relative_humidity_2m.inputs]
specific_humidity = { variable = "QAIR", product = "SHU" }
temperature = { variable = "TAIR", product = "TMP" }
pressure = { variable = "PAIR", product = "PRS" }
```

每个输入可添加 `units`，省略时读取文件属性。支持温度 K/℃、气压 Pa/hPa、水汽量 kg/kg 或 g/kg。`mixing_ratio` 是水汽与干空气的质量比，`specific_humidity` 是水汽与湿空气的质量比，两者必须按实际定义二选一；不能仅按字段名字猜测。若文件已有相对湿度，改用 `variable = "实际字段名"` 和 `units = "%"`（CLDAS 还需 `product`），并删除该要素的 `inputs` 表。

计算统一采用液态水饱和水汽压，低于 0℃ 也保持此口径。温度 T 使用 ℃，气压 p 使用 Pa：`es = 611.2 × exp(17.67 × T / (T + 243.5))`。混合比 r 对应 `e = p × r / (0.622 + r)`；比湿 q 对应 `e = p × q / (0.622 + 0.378 × q)`。计算 `100 × e / es` 后，派生湿度限制在 **0–100%**；直接映射的相对湿度也统一限制到此范围，避免不合理值进入曲线、指标或空间图。

派生湿度只接受同刻瞬时输入，不能用平均温度、平均气压等推算平均湿度。CLDAS 各产品独立选择最新制作版本，再按同一产品系列、严格相同有效时间组合；缺少同刻输入会提示并保留缺测，空间图跳过该时刻，整个时段无有效湿度则报错。点位检查三个输入的取值经纬度一致；空间图检查完整坐标轴一致，不进行插值或重网格。负水汽量、非正气压等无效输入会报错；文件缺测掩码继续传播，不补成零。

## 指标口径

瞬时数据按配置容差（默认 ±60 秒）在两条排序时间轴上进行一对一最近时次匹配，尽可能保留可配对的实况数量；每条实况和预报最多配对一次。时段量不使用时间容差：以实况时间窗为准，优先匹配相同窗口；也可把连续、完整、不重叠的较短预报时段合并后比较。均值按时长加权，雨量求和。缺口、部分重叠、不同时长的滚动窗口不插值、不拆分；不同实况时长分别输出结果。

连续指标包括 RMSE、MAE、Bias（预报减实况）和相关系数 R。气温另统计 `≤0℃` 与 `>0℃` 事件。降水事件由 `[metrics].precipitation_thresholds_mm` 中阈值定义，同一时段雨量达到阈值即为事件。事件指标输出命中、漏报、空报、正确否定、漏报率、空报率和空报比：漏报率 = 漏报/(命中+漏报)，空报率 = 空报/(空报+正确否定)，空报比 = 空报/(命中+空报)。没有分母时比率为空。

温度、湿度、降水和辐射均须相同时间含义后才能比较。当前已确认风速口径允许 WRF/CLDAS 输出时刻风速与站点 10 分钟平均风速按时间配对，图例会标明瞬时/平均；其他均值与瞬时值混用会拒绝计算指标。

## 数据路径与输出配置

所有运行配置集中在 `config.toml`。`[output].directory` 默认 `output`，是结果父目录；`[output].name` 默认 `evaluate`，作为运行目录前缀。每次成功运行生成独立的 `name_时间戳` 目录，不覆盖先前结果。复制进去的配置是本次原始 TOML 内容，CSV 不另加配对容差字段。

站点 Excel 必须由用户在 `sources.<名称>.files` 中逐个填写相对 `root` 的路径，程序不搜索或猜文件名。点位名称与经纬度直接由 `[[points]]` 配置；只有需要关联区域站 Excel 时才填对应 `station_id`。来源时区解释原始时间，`period` 必须填写带时区偏移的开始和结束时间；绘图时区由 `[output].timezone` 控制。

WRF 的 `root` 可以指向单次预报目录，也可以指向包含多个 `yyyymmddhh/` 子目录的总目录；`patterns = ["wrfout_d03*"]` 会递归匹配。起报时间优先使用路径中十位日期目录名，按该来源 `timezone` 解释。没有该目录名时，使用可选 `init_time`，否则自动读取文件属性 `SIMULATION_START_DATE`，缺少时回退 `START_DATE`。目录与文件模式起始时间不同会在进度中提示，仍以已确认的业务起报目录为准。续跑场景的文件属性未必等于业务起报，请使用目录约定或平铺来源的显式 `init_time`。

在 `[sources.wrf]` 设置 `min_lead_hours = 6` 即排除起报后前 6 小时的无效预报，恰好 6 小时保留；省略时默认 0。每个有效时刻选择满足条件的最新批次。降水等时段量先在各自批次内差分，完整窗口必须处于有效预报范围，再选能完整覆盖实况窗口的最新批次；不会跨批次拼接雨量或累计读数。最新批次数据缺测时保留缺测，不根据数值好坏改选旧批次。无法确定起报时间且设置了正的最低时效时会明确报错。

`max_lead_hours` 默认 168 小时（7 天），是用于目录筛选和文件内时次筛选的业务最长预报时效上限；如果业务最长预报时效变化，需要修改这一项。程序跳过“起报 + 最长时效早于 period”的整个目录，以及“起报 + 最低时效晚于 period”的整个目录，不打开这些目录中的 NetCDF；已打开文件中超出最长时效的时次也不会进入曲线或空间图。

标准 `wrfout_d03_YYYY-MM-DD_HH:MM:SS` 文件名（也支持下划线替代冒号及 `.nc` 后缀）用于下一层筛选：按已确认的数据规则，一个文件覆盖从文件名时刻到下一文件名时刻之前的时段。同目录、同起报、同 domain 内排序，只保留与有效检验窗口相交的文件，包括开始时刻之前可能装有分钟数据的前一个文件。没有下一文件的尾文件，使用起报加最长时效作为保守上界；未知起报时使用文件名加最长时效。无法解析文件名的候选文件回退内容检查。筛选仅读目录条目、文件名，不创建缓存；日志分别显示路径筛选数量和实际打开数量。

检查、扫描和元数据命令：

```bash
.venv/bin/python -m wrf_evaluate --config config.toml scan
.venv/bin/python -m wrf_evaluate --config config.toml inspect
.venv/bin/python -m wrf_evaluate --config config.toml match
```

`scan` 只统计文件，`inspect` 查看 WRF/CLDAS 元数据和配置点位，`match` 按 `period`、起报时效和文件名时间段筛选后检查 WRF 与 CLDAS 有效时间覆盖，不读变量场、不计算指标；运行中会输出带当前时间的进度。生成的 `output/` 和项目 `.venv/` 不应复制到服务器；在目标 Linux 服务器上用锁文件重新建立环境。

开发测试：`.venv/bin/python -m unittest discover -s tests -v`。Windows 将 `.venv/bin/python` 改为 `.venv/Scripts/python.exe`。
