# shared/llm/prompt_templates.py
# Prompt模板定义

from typing import Dict, Any, List


class PromptTemplates:
    """Prompt模板集合"""

    # ==================== QA类型专用模板 ====================

    # 协议理解类prompt模板
    QA_UNDERSTANDING_SYSTEM = """你是一个协议文档专家，专注于生成协议理解类问答对。

协议理解类问题聚焦：
- 字段定义与语义：字段名称、位宽、含义、用途
- 参数规格：数值范围、单位、精度、分辨率
- 结构关系：字段在消息中的位置、与其他字段的关联
- 规范说明：标准要求、约束条件、适用场景

输出要求：
1. 问题必须具体、可回答，避免模糊表述
2. 答案必须包含具体数值或参数（如位宽、范围、单位）
3. 答案应引用原文档中的关键信息
4. 问题和答案必须严格绑定当前文档中的真实字段、消息名或表项，不得生成与文档无关的通用题目
5. 禁止生成编程题、数学题、单位换算题、常识题或示例演示题
6. 同一字段只允许生成少量不同角度问题，禁止围绕同一字段反复改写
7. 对于参数规格类问题（范围/分辨率/单位/精度），答案必须补齐该字段的位宽；若文档未明确位宽，则不要生成这类问题

输出格式为JSON数组，每个元素包含：
- question: 具体问题（必须包含字段名或参数名）
- answer: 精确答案（必须包含数值或规格信息）
- qa_task_type: 固定为 "protocol_understanding" """

    QA_UNDERSTANDING_USER = """请根据以下协议文档内容，生成 {count} 个协议理解类问答对。

文档内容：
{content}

生成约束：
- 每个问题必须针对特定字段或参数
- 答案必须包含具体数值（位宽、范围、单位等）
- 优先关注：字段定义、位信息、数值范围、单位转换
- 如果问题询问范围、分辨率、单位或精度，答案中必须同时包含位宽；缺少位宽证据时不要生成该问答

请输出JSON数组格式的问答对。"""

    # 协议转换类prompt模板
    QA_CONVERSION_SYSTEM = """你是一个协议文档专家，专注于生成协议转换类问答对。

协议转换类问题聚焦：
- 数值转换：原始值与物理量之间的值到值转换公式
- 单位换算：不同单位之间的换算关系
- 编码解码：位编码与实际值的映射
- 跨协议转换：不同协议间相同语义的转换

输出要求：
1. 问题必须涉及转换关系或公式
2. 答案只能输出可执行的值到值公式（不要解释文字）
3. 公式必须可计算、可验证，可为单行表达式、mapping_table，或多行 if/else/for 代码块
4. 必须严格基于当前文档中真实出现的字段、映射或转换规则，不得输出通用编程公式、数学公式或单位换算题
5. 如果文档里没有明确转换关系，不要自行补充
6. 同一转换关系只允许生成一种问法，不要用不同说法重复改写

输出格式为JSON数组，每个元素包含：
- question: 关于转换关系的问题
- answer: 转换公式（仅公式，无解释）
- qa_task_type: 固定为 "protocol_conversion"
- conversion_mode: "transcoding"（语义转换）或 "mapping"（协议映射）
- conversion_formula: 与answer相同的公式字符串
- formula_kind: python_expr | python_block | mapping_table """

    QA_CONVERSION_USER = """请根据以下协议文档内容，生成 {count} 个协议转换类问答对。

文档内容：
{content}

生成约束：
- 每个问题必须涉及数值转换或单位换算
- 答案只能输出公式（如: value * 0.0013 / 60）
- conversion_mode判断：
  - transcoding: 同协议内语义转换（如原始值→物理量）
  - mapping: 跨协议或跨系统转换

请输出JSON数组格式的问答对。"""

    QA_QUESTION_PLANNING_SYSTEM = """你是一个协议文档专家，负责先从当前文档块中提出高质量问题，不要直接回答。

问题生成要求：
1. 只能围绕当前文档块中真实出现的字段、字名称、消息名、映射项、公式或规则提问
2. 禁止生成文档中没有证据支撑的问题
3. 每个问题都必须具体，不能泛泛而谈
4. 不允许围绕同一字段重复改写同一种问题
5. 如果是字段编码/枚举块，优先生成取值含义题；没有明确位宽/范围证据时，不要生成位宽题或范围题
6. 如果是 word map / message summary / reference layout 块，可以生成字名称、用途、位宽、范围、单位题，但前提是文档块中真的有对应证据
7. 问题类型必须尽量分散，不要把大多数问题都生成成“占多少位”这一种问法
8. 优先生成能得到信息量更高答案的问题，例如取值含义、范围、单位、分辨率、位段、用途、结构关系；位宽题只能占一部分
9. 如果同一字段既能问位宽，也能问范围、单位、分辨率或取值含义，优先后者，不要先把名额都耗在位宽题上
10. 只有在文档块中明确出现公式、映射或转换关系时，才生成 protocol_conversion 问题
11. 你会收到“允许题型目录”，请先从目录中挑选适合当前块的题型，再自行设计自然、具体的问题文本；不要照搬题型名称作为问题

输出格式必须是JSON数组，每个元素包含：
- question: 问题文本
- qa_task_type: protocol_understanding | protocol_conversion
- conversion_mode: transcoding | mapping | null
- source_field: 相关字段名或null
"""

    QA_QUESTION_PLANNING_USER = """请先阅读以下文档块内容，只生成 {count} 个候选问题，不要回答。

文档内容：
{content}

任务约束：
{task_spec}

补充要求：
{user_instruction}

额外要求：
- 尽量覆盖多种问题意图，如含义、范围、单位、分辨率、位段、用途、位宽、转换关系
- 除非没有别的证据支撑，不要让“多少位/位宽”类问题超过全部问题的一半
- 优先选择能让后续答案更完整、更具体的问题
- 根据当前块证据自行组织问题措辞，不要机械复用同一种句式

请直接输出JSON数组，不要输出任何额外文本。"""

    QA_ANSWER_GENERATION_SYSTEM = """你是一个协议文档专家，负责根据已给定的问题和当前文档块内容生成准确答案。

回答要求：
1. 只能使用当前文档块中明确出现的证据回答
2. 不得编造字段、位宽、范围、单位、公式或映射关系
3. 协议理解类问题：答案要紧扣字段规格或语义
4. 协议转换类问题：答案只能输出公式或映射关系，不要解释文字
5. 不要输出“未明确说明”“未指定”“未提供”“未知”“无法判断”这类空洞回答
6. 对协议理解类问题，如果文档证据不足以完整回答某个细项，可以结合协议字段命名、结构上下文和通用协议工程知识给出最佳专业回答，但必须保持保守，不要虚构具体公式或数值规则
7. 如果字段没有直接单位/分辨率说明，优先回答其编码语义、位宽、位段或可推断的表示方式，而不是直接说文档没写

输出格式必须是JSON对象，包含：
- question: 原问题
- answer: 回答
- qa_task_type: 原任务类型
- conversion_mode: 原conversion_mode或null
- conversion_formula: 如果是protocol_conversion则填写公式，否则为null
- source_field: 原source_field或null
"""

    QA_ANSWER_GENERATION_USER = """请根据以下文档块内容回答给定问题。

文档内容：
{content}

问题信息：
{question_payload}

请直接输出JSON对象，不要输出任何额外文本。"""

    QA_BATCH_ANSWER_GENERATION_SYSTEM = """你是一个协议文档专家，负责根据已给定的问题列表和当前文档块内容批量生成准确答案。

回答要求：
1. 只能使用当前文档块中明确出现的证据回答
2. 不得编造字段、位宽、范围、单位、公式或映射关系
3. 协议理解类问题：答案要紧扣字段规格或语义，优先给出信息更完整的答案，而不是只给一个最短碎片
4. 协议转换类问题：答案只能输出公式或映射关系，不要解释文字
5. 必须逐条回答给定问题，返回数量与输入问题数量一致
6. 如果问题涉及范围、单位、分辨率、位段或取值含义，答案应尽量带出同一字段的关键规格上下文，让回答可独立成立
7. 不要输出“未明确说明”“未指定”“未提供”“未知”“无法判断”这类空洞回答
8. 对协议理解类问题，如果细项证据不足，可以基于字段命名、结构位置、编码方式和通用协议工程知识给出最佳专业回答，但不要虚构公式或精确数值
9. 如果字段没有直接单位/分辨率说明，优先回答其表示方式、位宽、位段、枚举或编码语义，不要直接回答文档没写

输出格式必须是JSON数组，每个元素包含：
- question: 原问题
- answer: 回答
- qa_task_type: 原任务类型
- conversion_mode: 原conversion_mode或null
- conversion_formula: 如果是protocol_conversion则填写公式，否则为null
- source_field: 原source_field或null
"""

    QA_BATCH_ANSWER_GENERATION_USER = """请根据以下文档块内容批量回答问题列表。

文档内容：
{content}

问题列表：
{questions_payload}

请直接输出JSON数组，不要输出任何额外文本。"""

    QA_FAST_BATCH_GENERATION_SYSTEM = """你是一个严谨的协议文档数据标注员，负责基于多个文档片段一次性生成高质量问答对。

任务要求：
1. 你会收到多个片段，每个片段都带有唯一的 [SEGMENT_ID]
2. 必须针对每个片段生成多个候选 QA，并在每条结果中带回对应的 segment_id
3. 问题应覆盖不同的大类题型，例如：字段含义、位宽/位段、范围、单位、分辨率、枚举取值含义、结构关系、转换/映射关系
4. 不同片段应根据各自内容自由选择适合的题型，不要让所有片段都只生成同一种问法
5. 优先围绕具体字段名、具体数值、具体枚举值、具体位段、具体规则来提问并回答
6. 严禁输出“未明确说明”“未指定”“未提供”“未知”“无法判断”“不详”等空洞回答
7. 如果文档信息不完整，可以结合上下文和通用协议工程知识给出保守但具体的专业回答，但不要虚构精确公式或不存在的数值
8. 对转换类问题，answer 必须输出具体公式、映射关系或明确转换规则，不要写空泛解释

输出格式必须是 JSON 数组，每个元素包含：
- segment_id: 片段ID
- question: 问题
- answer: 回答
- qa_task_type: protocol_understanding | protocol_conversion
- conversion_mode: transcoding | mapping | null
- conversion_formula: 转换公式或null
- source_field: 字段名或null
"""

    QA_FAST_BATCH_GENERATION_USER = """请根据以下多个文档片段生成候选问答对。

全局要求：
- 本批次目标候选总数：{candidate_count}
- 每个片段都有自己的建议候选数，请尽量覆盖全部片段
- 题型应分散，不要机械重复同一问法
- 优先生成答案更完整、信息量更高的问答
- 只输出JSON数组，不要输出任何额外文本

片段级配额参考：
{segment_quota_text}

题型目录（只给大类，不要照抄成问题）：
- 字段含义
- 位宽或位段
- 数值范围
- 单位或分辨率
- 枚举取值含义
- 结构或布局关系
- 转换公式或映射关系

补充要求：
{user_instruction}

文档片段：
{content}
"""

    # 关键词检索映射
    QA_TYPE_KEYWORDS = {
        "protocol_understanding": [
            "field", "字段", "bit", "位", "width", "宽度", "range", "范围",
            "unit", "单位", "resolution", "分辨率", "meaning", "含义",
            "WORD", "message", "label", "signal", "parameter"
        ],
        "protocol_conversion": [
            "formula", "公式", "convert", "转换", "calculation", "计算",
            "mapping", "映射", "transcoding", "转义", "coefficient", "系数",
            "value", "数值", "multiplier", "乘数", "frequency", "频率",
            "latitude", "纬度", "longitude", "经度", "speed", "速度"
        ]
    }

    # ==================== QA抽取模板 ====================

    QA_EXTRACT_SYSTEM = """你是一个协议文档专家，专注于从问答文本中抽取结构化的字段信息。
请从用户提供的问答内容中提取出协议字段的技术参数，包括：
- field_name: 字段名称
- bit_width: 位宽（整数）
- bit_start: 起始位（整数，可选）
- resolution: 分辨率（浮点数）
- unit: 单位
- range_min: 最小值
- range_max: 最大值
- meaning: 字段语义说明
- conversion_formula: 转换公式（如存在）

请以JSON格式输出提取结果。如果某项信息未提及，设为null。"""

    QA_EXTRACT_USER = """请从以下问答内容中提取字段信息：

问题：{question}
回答：{answer}
协议类型：{protocol_type}

请输出JSON格式的提取结果。"""

    # ==================== QA生成模板 ====================

    QA_GENERATE_SYSTEM = """你是一个协议文档专家，需要根据文档内容生成高质量问答对。
任务类型要求：
1. protocol_understanding（协议理解类）：聚焦字段定义、位宽、范围、含义、单位。
2. protocol_conversion（协议转换类）：聚焦跨语义/跨协议转换。
   - conversion_mode=transcoding：不同语义通过公式转换（转义）
   - conversion_mode=mapping：不同协议同一语义的转换公式（转换）
   - 对于 protocol_conversion，answer 只能输出值到值转换公式（不要解释文字）
   - 允许单行表达式、mapping_table，或多行 if/else/for 代码块；多行时最终值必须赋给 result

输出格式为JSON数组，每个元素包含：
- question: 问题
- answer: 答案（转换类仅公式）
- qa_task_type: protocol_understanding | protocol_conversion
- conversion_mode: transcoding | mapping | null
- conversion_formula: 公式字符串或null
- formula_kind: python_expr | python_block | mapping_table | null

输出约束（必须遵守）：
- 只能输出一个JSON数组，禁止输出任何解释文字、注释、前后缀。
- 禁止输出<think>、推理过程、Markdown代码块。"""

    QA_GENERATE_USER = """请根据以下文档内容生成 {count} 个问答对：

文档内容：
{content}

任务约束：
{task_spec}

{user_instruction}

请直接输出JSON数组，不要输出任何额外文本。"""

    # ==================== 语义分块模板 ====================

    SEMANTIC_CHUNK_SYSTEM = """你是一个文档分析专家，需要分析文本内容的语义结构，判断哪些内容块属于同一个语义单元。

语义单元的类型包括：
- field_definition: 字段定义（包含字段名称、位宽、范围等）
- conversion_rule: 转换规则（包含计算公式、映射关系等）
- protocol_description: 协议描述（包含协议概述、用途等）
- table_data: 表格数据（结构化的数值表）
- code_example: 代码示例

请分析内容块的关联性，输出语义分块建议。"""

    SEMANTIC_CHUNK_USER = """请分析以下内容块的语义关联性：

{blocks}

请判断哪些块应该合并为同一个语义单元，输出JSON格式的分块建议：
[{{"block_ids": [id列表], "semantic_type": "类型", "reason": "合并原因"}}]"""

    # ==================== 质量检测模板 ====================

    QUALITY_CHECK_SYSTEM = """你是一个问答质量评估专家，需要判断问答对的质量。

高质量的问答对应该：
1. 问题明确具体
2. 答案完整准确
3. 包含有价值的技术信息

低质量的问答对可能：
1. 问题过于宽泛或模糊
2. 答案过短或无实质内容
3. 包含错误信息
4. 缺乏具体数值或参数

请评估并输出JSON格式结果。"""

    QUALITY_CHECK_USER = """请评估以下问答对的质量：

问题：{question}
答案：{answer}

请输出JSON格式：{{"is_low_quality": true/false, "reason": "原因"}}"""

    # ==================== 规则校验模板 ====================

    VALIDATION_RULES: Dict[str, Dict[str, Any]] = {
        "RangeCoverageCheck": {
            "description": "量程覆盖校验",
            "check": lambda info: (
                info.get("bit_width") is not None
                and info.get("range_min") is not None
                and info.get("range_max") is not None
                and 2 ** info["bit_width"] >= (info["range_max"] - info["range_min"])
            ),
            "pass_msg": lambda info: f"{info.get('bit_width')}位宽可表示范围覆盖[{info.get('range_min')}, {info.get('range_max')}]",
            "fail_msg": "位宽不足以覆盖指定范围",
        },
        "BitWidthFormat": {
            "description": "位宽格式校验",
            "check": lambda info: (
                info.get("bit_width") is not None
                and isinstance(info["bit_width"], int)
                and info["bit_width"] > 0
            ),
            "pass_msg": lambda info: "位宽为正整数",
            "fail_msg": "位宽必须为正整数",
        },
        "ResolutionFormat": {
            "description": "分辨率格式校验",
            "check": lambda info: (
                info.get("resolution") is None
                or (isinstance(info["resolution"], (int, float)) and info["resolution"] > 0)
            ),
            "pass_msg": lambda info: f"分辨率格式正确: {info.get('resolution')}",
            "fail_msg": "分辨率必须为正数",
        },
        "RangeFormat": {
            "description": "范围格式校验",
            "check": lambda info: (
                info.get("range_min") is None
                or info.get("range_max") is None
                or info["range_min"] < info["range_max"]
            ),
            "pass_msg": lambda info: f"范围设置合理: [{info.get('range_min')}, {info.get('range_max')}]",
            "fail_msg": "最小值应小于最大值",
        },
    }

    @classmethod
    def format_qa_extract(cls, question: str, answer: str, protocol_type: str = "") -> tuple:
        """格式化QA抽取prompt"""
        system = cls.QA_EXTRACT_SYSTEM
        user = cls.QA_EXTRACT_USER.format(
            question=question,
            answer=answer,
            protocol_type=protocol_type,
        )
        return system, user

    @classmethod
    def format_qa_generate(
        cls,
        content: str,
        count: int = 5,
        system_prompt: str = None,
        user_instruction: str = None,
        task_spec: str = None,
    ) -> tuple:
        """格式化QA生成prompt"""
        if system_prompt:
            # 用户自定义系统提示作为补充，不覆盖基础结构化约束
            system = f"{cls.QA_GENERATE_SYSTEM}\n\n补充要求：\n{system_prompt}"
        else:
            system = cls.QA_GENERATE_SYSTEM
        user = cls.QA_GENERATE_USER.format(
            content=content[:4000],  # 限制长度
            count=count,
            task_spec=task_spec or "默认混合生成协议理解类与协议转换类问答。",
            user_instruction=user_instruction or "",
        )
        return system, user

    @classmethod
    def format_quality_check(cls, question: str, answer: str) -> tuple:
        """格式化质量检测prompt"""
        return cls.QUALITY_CHECK_SYSTEM, cls.QUALITY_CHECK_USER.format(
            question=question,
            answer=answer,
        )

    @classmethod
    def format_semantic_chunk(cls, blocks: List[Dict[str, Any]]) -> tuple:
        """格式化语义分块prompt"""
        blocks_text = "\n".join([
            f"[Block {b.get('block_id', i)}]: {b.get('content', '')[:500]}"
            for i, b in enumerate(blocks)
        ])
        return cls.SEMANTIC_CHUNK_SYSTEM, cls.SEMANTIC_CHUNK_USER.format(blocks=blocks_text)

    @classmethod
    def get_validation_rules(cls, protocol_type: str = "") -> Dict[str, Dict[str, Any]]:
        """获取校验规则"""
        # 可根据协议类型返回不同规则
        return cls.VALIDATION_RULES

    # ==================== QA类型专用方法 ====================

    @classmethod
    def format_qa_understanding(cls, content: str, count: int = 3) -> tuple:
        """格式化协议理解类QA生成prompt"""
        user = cls.QA_UNDERSTANDING_USER.format(
            content=content[:4000],
            count=count,
        )
        return cls.QA_UNDERSTANDING_SYSTEM, user

    @classmethod
    def format_qa_conversion(cls, content: str, count: int = 3) -> tuple:
        """格式化协议转换类QA生成prompt"""
        user = cls.QA_CONVERSION_USER.format(
            content=content[:4000],
            count=count,
        )
        return cls.QA_CONVERSION_SYSTEM, user

    @classmethod
    def format_qa_question_planning(
        cls,
        content: str,
        count: int,
        task_spec: str = "",
        user_instruction: str = "",
    ) -> tuple:
        user = cls.QA_QUESTION_PLANNING_USER.format(
            content=content[:4000],
            count=count,
            task_spec=task_spec or "优先提出协议理解类问题。",
            user_instruction=user_instruction or "只提问，不回答。",
        )
        return cls.QA_QUESTION_PLANNING_SYSTEM, user

    @classmethod
    def format_qa_answer_generation(
        cls,
        content: str,
        question_payload: str,
    ) -> tuple:
        user = cls.QA_ANSWER_GENERATION_USER.format(
            content=content[:4000],
            question_payload=question_payload,
        )
        return cls.QA_ANSWER_GENERATION_SYSTEM, user

    @classmethod
    def format_qa_batch_answer_generation(
        cls,
        content: str,
        questions_payload: str,
    ) -> tuple:
        user = cls.QA_BATCH_ANSWER_GENERATION_USER.format(
            content=content[:4000],
            questions_payload=questions_payload,
        )
        return cls.QA_BATCH_ANSWER_GENERATION_SYSTEM, user

    @classmethod
    def format_qa_fast_batch_generation(
        cls,
        content: str,
        candidate_count: int,
        segment_quota_text: str,
        user_instruction: str = "",
    ) -> tuple:
        user = cls.QA_FAST_BATCH_GENERATION_USER.format(
            content=content[:12000],
            candidate_count=candidate_count,
            segment_quota_text=segment_quota_text or "- 无",
            user_instruction=user_instruction or "围绕真实字段和规则生成具体问答。",
        )
        return cls.QA_FAST_BATCH_GENERATION_SYSTEM, user

    @classmethod
    def get_qa_type_for_content(cls, content: str) -> str:
        """根据内容关键词判断适合的QA类型"""
        content_lower = content.lower()

        understanding_score = sum(
            1 for kw in cls.QA_TYPE_KEYWORDS["protocol_understanding"]
            if kw.lower() in content_lower
        )
        conversion_score = sum(
            1 for kw in cls.QA_TYPE_KEYWORDS["protocol_conversion"]
            if kw.lower() in content_lower
        )

        # 优先选择转换类（因为当前转换类QA较少）
        if conversion_score >= 2:
            return "protocol_conversion"
        elif understanding_score >= 2:
            return "protocol_understanding"
        else:
            return "protocol_understanding"  # 默认理解类

    @classmethod
    def filter_chunks_by_qa_type(
        cls,
        chunks: List[Dict[str, Any]],
        qa_type: str,
        top_k: int = 5
    ) -> List[Dict[str, Any]]:
        """根据QA类型筛选相关chunks"""
        keywords = cls.QA_TYPE_KEYWORDS.get(qa_type, [])
        if not keywords:
            return chunks[:top_k]

        scored_chunks = []
        for chunk in chunks:
            content = chunk.get("content", "").lower()
            score = sum(1 for kw in keywords if kw.lower() in content)
            scored_chunks.append((score, chunk))

        # 按分数降序排序
        scored_chunks.sort(key=lambda x: x[0], reverse=True)
        return [chunk for score, chunk in scored_chunks[:top_k] if score > 0]

    @classmethod
    def format_qa_by_type(
        cls,
        content: str,
        qa_type: str,
        count: int = 3
    ) -> tuple:
        """根据QA类型选择对应的prompt模板"""
        if qa_type == "protocol_conversion":
            return cls.format_qa_conversion(content, count)
        else:
            return cls.format_qa_understanding(content, count)
