# WRF 预报检验

对比 WRF、CLDAS 与站点实测，输出时序叠图、WRF/CLDAS/差值三列空间填色图和指标 CSV。直接只读原始 WRF、NetCDF 和 Excel，不转换数据、不创建持久数据缓存，不生成网页或文档报告。网格点位使用最近格点；曲线各来源保留自身时间分辨率，画在同一个坐标轴内，只有指标计算才配对。

支持2m气温、2m相对湿度、风速、降水、地表向下短波和长波辐射。EC 尚未接入。区域站、国家站统一在 `[[station]]` 中配置，每站必须指定 Excel。目前读取已核实的长表布局：默认 `气象数据` 工作表（可改）、A1 表头、每行一个站点/时刻，文本时间格式可配置，默认14位年月日时分秒；也支持Excel日期单元格。已核实的国家站月表也是该长表布局，可通过每站表头映射统一读取。

## 运行

Python 3.11+，依赖通过 uv 安装到项目 .venv，不安装到系统 Python：

```bash
uv sync --locked
uv run main.py plot
# 服务器
uv run main.py --config config.server.toml plot
```

运行plot会先自动校验配置、文件路径和任务时间口径，错误直接输出日志并以非零状态结束，不生成状态报告。Linux 无桌面环境也可绘图。部署时复制 `fonts/SimHei.ttf`，所有图优先使用它；缺少时回退系统字体。不要复制本地 .venv、.uv-cache 和 output，在服务器用锁文件重新建立环境。

## 两类配置

日常选择一个运行文件，通过顶层 source_config 引用稳定来源定义。不递归包含、不做多份覆盖。

| 文件 | 内容 |
| --- | --- |
| config.toml | 本地本次运行设置，引用 config.sources.local.toml |
| config.server.toml | 服务器本次运行设置，引用 config.sources.toml |
| config.sources.toml | 服务器 WRF/CLDAS 路径、来源时区、字段定义与显示名 |
| config.sources.local.toml | 本地 WRF/CLDAS 样例的来源定义 |

source_config、output.directory 和 station.excel.files 相对运行文件解析，也支持绝对路径；来源 root 相对来源文件解析。运行文件与来源文件不能混写，未知配置项会报错。站点 Excel 文件、时区、工作表、表头规则均放在本次运行的对应站点下，不再放进 source_config。

每次在 `output/<name>_<时间戳>/` 创建独立目录。output.directory 默认 output，name 默认 evaluate，显示时区默认 Asia/Shanghai。保存本次实际加载的两份原始配置副本：运行配置为 config.toml，来源配置保留原文件名（若也叫 config.toml，改存 config.sources.toml）。保存加载时的字节内容，运行中修改配置不会影响副本。副本用于追溯；从输出目录重跑需核对配置引用和相对路径。CSV 不另放配对容差等运行配置字段。

## 运行配置：配置几组就执行几组

```toml
source_config = "config.sources.toml"

[period]
# 同一时段的UTC写法：start = "2024-12-01T04:00:00Z"、end = "2024-12-07T04:00:00Z"。
start = "2024-12-01T12:00:00+08:00"
end = "2024-12-07T12:00:00+08:00"

[output]
name = "wrf-test"

[[station]]
name = "水西沟"
longitude = 87.4897
latitude = 43.4733
# station_id = "Y5012" # 单站工作表可省略；多站工作表须填写以筛选。
time_tolerance_seconds = 60 # 默认60，非负整数秒；0严格同刻，仅影响瞬时指标。
# excel.files必填，缺失或文件不存在会在运行前报错。
excel.files = ["/data/station/selected.xls"] # 明确列出文件，可多个；不搜索目录。
excel.timezone = "UTC" # 默认UTC，按原始数据实际时区填写。
excel.sheet = "气象数据" # 默认“气象数据”；只支持A1表头的长表。
excel.time_format = "%Y%m%d%H%M%S" # 文本格式；也可用"%Y-%m-%d %H:%M:%S"，Excel日期单元格自动识别。
excel.missing_values = [] # 缺测标记，例[-9999, "M"]，只填数据实际使用的标记。
excel.variables.station_id = { variable = "区站号" }
excel.variables.time = { variable = "观测时间" }
excel.variables.temperature_2m = { variable = "气温(°)", units = "degC", time_kind = "instant" }
# 其他要素表头按相同方式填写；完整参考见config.toml。

[[plots]]
element = "temperature_2m"
type = "series"
figsize = [11, 4.8] # 英寸，series默认[11, 4.8]。
dpi = 160 # 正整数，默认160。
# y_limits = [-30, 10] # 可选，纵轴范围；省略自动缩放。

[[plots]]
element = "temperature_2m"
type = "map"
interval = "6h" # 默认6h，可填正整数+s/min/h/d。
figsize = [12, 5] # 整图宽度、每行高度，英寸；默认[12, 5]。
dpi = 160 # 正整数，默认160。
cmap = "coolwarm" # 默认viridis；支持Matplotlib色表，如coolwarm、RdBu_r、turbo。
# color_limits = [-30, 10] # 可选，所有时刻预报/实况共用色标；省略按全部数据自动取值。
layout = "timeline" # 默认timeline：所有时刻同图；separate：每个时刻单独一张图。
difference_cmap = "RdBu_r" # 差值色表，默认RdBu_r。
# difference_limit = 5 # 可选，所有差值共用[-5, 5]色标；省略按全部时刻自动对称取值。

[[metrics]]
element = "temperature_2m"
# 气温指标没有额外参数；容差由每个station指定。
# 仅precipitation可设置precipitation_thresholds_mm = [0.1, 1, 10]。
```

每个 [[plots]] 就地设置变量、图类型和该图参数。一组 series 任务为每个配置点位分别出图；一组map任务按时段与间隔组织各行，默认汇总成一张图。本地CLDAS样例仅含北京时间12月24日08:00、09:00，测试空间图时需让period.start加interval落在这些时刻（如period从07:00开始、interval="1h"）；降水还需完整窗口在period内。同变量可配置多组任务，例如不同色表、范围、间隔，文件名带任务序号，互不覆盖。图范围只影响显示，不改变指标数据。

增加站点时复制一组 [[station]]，修改名称、坐标和 Excel 规则；现有 series/metrics 任务会自动分别处理所有站点，无须为每个站点重复配置任务。示例运行配置已包含水西沟与东梁村两个站点，以及气温、湿度、风速、降水的时序图、空间图和指标任务。

[[metrics]] 独立配置指标，每个变量最多一组。不需要的任务删除或整组注释，不使用 enabled。未配置的变量或图类型不执行；至少配置一项任务。只有指标不画图；只有空间图可省略 station，也不读取站点 Excel。多图共享索引、点位序列和同刻空间场。

| 任务 | 参数与默认值 |
| --- | --- |
| plots，type = "series" | figsize = [11, 4.8]、dpi = 160、可选 y_limits |
| plots，type = "map" | interval、figsize（总宽、每行高）、dpi、cmap、color_limits、layout、difference_cmap、difference_limit |
| metrics | 仅降水可设 precipitation_thresholds_mm = [0.1, 1, 10]，默认空数组，不计算降水事件率 |

空间图默认interval="6h"、figsize=[12, 5]（整图宽、每行高）、dpi=160、cmap="viridis"、layout="timeline"、difference_cmap="RdBu_r"；color_limits和difference_limit默认按本图全部时次自动确定。

尺寸单位英寸，范围使用要素统一单位。所有任务共用 period 和 output.timezone。时间必须含时区偏移，+08:00 是北京时间、Z 是 UTC，包含端点。站点名称和经纬度直接填写，不读取位置 Excel。单站工作表自动从表内识别站号，顶层 station_id 可省略；表内有多个站号时须显式指定，跨文件读到不同站号会报错，不能混用。excel.variables.station_id 仍是表头映射，用于读取和核对站号。station.time_tolerance_seconds 对本站所有瞬时指标生效，默认60秒，允许0；也适用于本站的 WRF/CLDAS 指标，不改变曲线、不放宽时段量的窗口匹配。

仅支持当前两文件配置格式，旧单文件、[[points]]、elements.*.outputs、matching、maps等入口不再兼容；未知配置项在运行前报错。来源文件只包含网格 sources 和 elements，不放任何站点 Excel 或绘图/指标参数。

## 来源和字段映射

```toml
[sources.wrf]
kind = "wrf"
root = "/data/wrf"
patterns = ["wrfout_d03*"]
timezone = "UTC"
lead_hours = [6, 168]

[sources.cldas]
kind = "cldas"
root = "/data/cldas"
patterns = ["*.nc"]
timezone = "UTC"

[elements.temperature_2m]
name = "2m气温"
sources.wrf = { variable = "T2" }
sources.cldas = { product = "TMP", variable = "TAIR" }
```

网格来源名称可以自定义，变量映射中的名称须与它对应。区域站、国家站共用station.excel读取流程。已核实富蕴、昌吉国家站月表也是A1长表，无需额外读取器：sheet设为sheet1，站号/时间/气温/湿度/风速表头按文件映射，时区按实际设置为Asia/Shanghai；其他布局须另行适配。

Excel 只读取本站 excel.files 指定的文件，不搜索或猜文件名。同站多个文件共用一套 excel.variables 规则；不同站点的表头、单位、时间含义、工作表和时区互不影响。省略的已知字段使用已核实样例默认定义，非默认辐射列须明确映射并填写单位。缺测标记先转换为缺测，再换单位、限制湿度范围和计算指标；空白自动视为缺测，不猜测9999等特殊数值。文本时间格式采用Python strptime写法（%Y年、%m月、%d日、%H时、%M分、%S秒），默认格式有直接解析快路径。同一文件被多个站点引用且规则相同时只读取一次，先筛站号和时段再解析气象值；同站号在不同站点的文件中不会串用。字段名称须使用上表的统一英文要素名。

风速统一名为 `wind_speed`，不限定 Excel 风速高度或平均方式。程序只读取用户映射的风速列；若换成瞬时风速，设置 time_kind="instant"；若换成2分钟平均风速，设置 mean、interval="2min"、timestamp="end"。WRF 默认由 U10/V10 合成10m风速；跨来源比较口径由用户选定的字段决定，程序不把瞬时风速转换成平均风速。

**CLDAS product 是文件名中的产品代码**（TMP、PRE），**variable 是 NetCDF 内字段名**（TAIR、PRCP）。先用 product 找文件，再用 variable 读字段，来自不同命名体系，不能合并成同一个值。WRF 的 variable 也是文件内字段，Excel 则是表头。

## 统一 time_kind

所有来源共用四类时间含义，名称差异表示物理含义差异：

| time_kind | 含义 | 配置与处理 |
| --- | --- | --- |
| instant | 瞬时值 | 默认值，不设置 interval/timestamp |
| mean | 时段平均值 | 须设置 interval 和 timestamp |
| sum | 时段内总量 | 须设置 interval 和 timestamp，直接使用时段量 |
| cumulative | 从某起算点不断累加的读数 | 同批次相邻时次差分，不设置 interval/timestamp |

interval 为正整数加 s/min/h/d，timestamp = "end" 表示记录时刻是窗口终点，"start" 表示起点。温度、湿度、风速支持 instant/mean；降水支持 sum/cumulative；辐射支持 instant/mean，能量字段可用 sum/cumulative。

```toml
[elements.precipitation]
sources.wrf = { variable = "RAINNC", units = "mm", time_kind = "cumulative" }
sources.cldas = { product = "PRE", variable = "PRCP", time_kind = "sum", interval = "1h", timestamp = "end" }
# 站点原值映射放在运行文件的对应[[station]]下：
# excel.variables.precipitation = { variable = "小时降水量(mm)", units = "mm", time_kind = "sum", interval = "1h", timestamp = "end", window = "hour_to_date" }
```

WRF 累计量需要差分，CLDAS 已是过去一小时的时段雨量，不能再次差分。因此分别用 cumulative 和 sum，处理后比较同一时段。降水只读指定的一个字段，不自动将 RAINC、RAINNC、RAINSH 相加。样例 WRF 属性明确这些都是累计 mm，但不能据此解释 Excel。

WRF 点位降水先在单一起报内按原生时次差分，再合成完整、不重叠的整点逐小时量。分钟读数可以合成小时量，小时量不会拆成分钟量。跨起报、缺口、不足一小时或越过有效预报窗口的区间不冒充完整小时量；负差分报错，不猜清零规则。起点08:00时，第一段08:00–09:00绘在09:00。

两站样例的雨量下降均发生在整点后第5分钟，支持“本小时累计、整点为刚结束的一小时总量”的判断。站点降水采用 sum，另设 window="hour_to_date"（仅适用1h、end）：10:35表示10:00–10:35雨量，11:00表示10:00–11:00雨量。曲线保留原值和原时间分辨率；WRF已合成为逐小时雨量，因此站点只有完整小时记录参与降水指标，不对小时内读数相加或重复差分。若提供方定义为过去一小时滚动雨量，改为 window="rolling"；显式设置 time_kind/interval/timestamp 而省略 window 时也采用 rolling。该解释基于现有样例，提供方的正式说明应优先。

降水统一 mm。mm/h 仅在明确为 sum 且已配置窗口时，按时段平均雨强乘时长转换；瞬时雨强不能据此推算。辐射通量统一 W/m²；时段或累计能量 J/m² 按真实时间窗秒数换算平均通量，不只换标签。

## 相对湿度和 WRF 网格

相对湿度可直接映射，也可由基础变量派生。WRF 默认 Q2/T2/PSFC，CLDAS 默认 SHU/QAIR、TMP/TAIR、PRS/PAIR：

```toml
[elements.relative_humidity_2m]
sources.wrf.inputs.mixing_ratio = { variable = "Q2" }
sources.wrf.inputs.temperature = { variable = "T2" }
sources.wrf.inputs.pressure = { variable = "PSFC" }
sources.cldas.inputs.specific_humidity = { variable = "QAIR", product = "SHU" }
sources.cldas.inputs.temperature = { variable = "TAIR", product = "TMP" }
sources.cldas.inputs.pressure = { variable = "PAIR", product = "PRS" }
# 站点相对湿度映射放在运行文件的对应[[station]]下：
# excel.variables.relative_humidity_2m = { variable = "相对湿度(%RH)", units = "%" }
```

各输入可设 units，省略时读文件属性。支持温度 K/℃、气压 Pa/hPa、水汽量 kg/kg 或 g/kg。mixing_ratio 是水汽与干空气质量比，specific_humidity 是水汽与湿空气质量比，不能互换。改用直接相对湿度字段时删除 inputs，填写 variable 和 units（CLDAS 还需 product）。

计算统一采用液态水饱和水汽压，低于0℃也保持该口径。T 为℃、p 为 Pa：es = 611.2 × exp(17.67 × T / (T + 243.5))；混合比 r 对应 e = p × r / (0.622 + r)，比湿 q 对应 e = p × q / (0.622 + 0.378 × q)。相对湿度为100 × e / es，直接与派生湿度均限制在0–100%。

派生湿度只接受同刻瞬时输入，不能用平均基础量推算平均湿度。CLDAS 各产品选最新制作版本后按同系列、严格同刻组合。缺输入保留缺测，空间图跳过该时刻；输入网格不一致报错，不插值。输入掩码合并；负水汽量、非正气压、比湿不在[0,1) kg/kg、温度不高于-243.5℃的位置保留缺测，不补成零。日志报告异常字段、数量、值域与单位，整个时段或比较区域无有效值仍报错。

WRF T2、U10/V10、降水和地面辐射已在质量网格上，无需去交错。10m风速按 hypot(U10,V10) 计算，可配置 components；旋转不改变风速大小。三维 U/V/W、垂直层和风向旋转尚未接入，误映射报错，不把最低模式层风当成10m风。累计量不跨起报或移动格点相减。

## 空间图与指标

空间图以period.start为基准，按每组interval安排时刻。默认layout="timeline"，每行一个时刻、三列依次为WRF、CLDAS、WRF−CLDAS；也可用separate逐时刻出图。所有行的预报和实况共用一条色标，差值另用一条以0对称的统一色标。figsize为整图宽度和每行高度，总高度随时刻数增加；较长时段可增大interval或采用separate。

范围取WRF网格外接矩形，CLDAS只读含边缘相邻格点的子区域。前两列保留各自原网格；仅差值在WRF格点取球面最近CLDAS格点，计算WRF−CLDAS，不对缺测格点改选邻点，CLDAS域外保持缺测，暂不含行政边界或空间指标。缺少严格对应时刻或完整时段会在日志说明，汇总图对应行标注无完整数据；全时段无可画数据则不生成空图。降水/辐射比较同一窗口，出图间隔不是累计时长，例如每6小时比较此前1小时雨量。窗口超出period时不绘制该行。

瞬时指标默认±60秒内最近时间一对一匹配，每条预报、实况最多用一次；等距优先较早实况、较早预报，不保证全局配对数量最大。时段量不用容差，匹配相同窗口或合并连续完整、不重叠的较短预报窗口；均值按时长加权，雨量求和。不插值、不拆分滚动或部分重叠窗口。允许 WRF/CLDAS 瞬时风速与站点10分钟平均风速配对，图例标明口径；其他瞬时与均值混用拒绝计算指标。

连续指标为 RMSE、MAE、Bias（预报减实况）和相关系数 R。气温另统计非正温≤0℃、正温>0℃事件。降水达到 metrics 任务配置阈值即为事件，输出命中、漏报、空报、正确否定及：

- 漏报率 = 漏报/(命中+漏报)
- 空报率 = 空报/(空报+正确否定)
- 空报比 = 空报/(命中+空报)

分母为零或统计未定义时留空。日志显示配对数和有效样本数，无有效样本时提示核对时间覆盖、窗口和缺测。

## 读取、筛选与进度

日志即时输出，前缀为 [YYYY-MM-DD HH:MM:SS]，使用输出时区。Excel 初次加载或单次底层读取只能显示当前阶段，不能报告字节级进度。绘图前报告各曲线首末有效时次、有效点数及开头缺口；横轴固定period，不平移曲线、不填缺测；采样间隔有严格多数的固定频率时，超过3倍常见间隔的空档断线显示，不跨缺口连线。

WRF root 可指向单次预报或含多个 yyyymmddhh 目录的总目录，递归匹配 patterns。起报优先取十位目录名，按来源 timezone 解释；平铺文件使用可选 init_time，否则读取 SIMULATION_START_DATE，缺少时回退 START_DATE。目录与属性不一致会提示，仍以目录业务起报为准。续跑属性未必等于业务起报，需目录约定或显式 init_time。

lead_hours = [最小时效, 最大时效]，单位小时，默认 [0, 168]，包含两端；例如 [6, 168] 排除前6小时，恰好6小时保留，并限制至第168小时（7天）。同一有效时刻选最新合格起报，旧报补足新报没有的时刻；新报已有时刻但数值缺测则保留缺测，不按数值好坏换旧报。时段量由同一起报完整覆盖窗口，不跨批次拼接。最大时效也用于跳过不可能覆盖 period 的起报目录、筛选文件内时次，业务预报上限变化时需修改。

标准 wrfout 文件覆盖 [本文件名时间,下一文件名时间)，据此筛候选，保留开始前可能含分钟数据的文件；末文件用起报加最长时效作为保守上界。无法解析名称时回退内容核对。只有空间图时只索引实际需要窗口；点位曲线保留原生时次和差分所需数据。被新批次覆盖的旧瞬时字段不再取值，时段量保留完整窗口。支持软链接并防止目录递归成环。

CLDAS 先扫文件名，筛产品、有效时间、最新制作版本，再按需读点位或空间子区域。yyyymmdd 目录是制作/下载日期，不能按它裁剪有效日期。派生湿度缺项时不读其余无用产品。多图共享同刻场；WRF 同文件、同帧复用读取结果，换文件关闭句柄、换帧释放场，不缓存整个时段。点位定位仅在内存复用上一帧，经纬度或掩码变化时重新定位。Excel 每个来源文件只开一次，先筛站号、时段再解析选中字段。

其他核查命令：`uv run main.py scan`、`uv run main.py inspect`、`uv run main.py match`。scan 统计文件，inspect 核查元数据，match 检查时间候选覆盖，不读气象场、不算指标。开发测试：`uv run python -m unittest discover -s tests`。
