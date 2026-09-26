## 代码说明
main.py: 批量处理文件的脚本入口

map_generic_component.py：匹配最佳骨架的函数，用于在通式中有多个产物时，找到实施例对应的应该是哪个骨架

reconstruct_reaction.py：主要的算法逻辑，包括输入骨架和实施例确定R_group的函数，以及将R_group代回其他骨架的函数

utils.py：其他共用函数

## 整体逻辑解释
1. 使用utils.smiles_to_graph，将反应式中所有smiles转换为图结构，包括通式反应物、通式产物、催化剂、所有实施例（如果可行的话），无法转换的会跳过。这里为了进行环匹配，会在被取代的环上用特殊的记号在环上所有位点插上R原子，被环取代转换为单取代问题。
2. 通式反应物、通式产物、催化剂的图结构放入G_generals中，实施例的图结构放入G_examples中
3. 在通式产物中用find_best_candidate进行匹配，寻找与实施例最相似的那个骨架（如果有多个产物）。匹配逻辑是基于MCS（最大公共子结构）的比例，当骨架分子非常简单时可能会匹配到错误的分子（如R1-C-C-R2这类骨架，MCS会很小导致匹配失败）。
4. 对于每个实施例，将其与3中匹配到的骨架G_general进行对比，寻找R_groups和对应代回（以下称为Rp_group），具体逻辑为
   1. 确定G_general和G_example的MCS
   2. 用MCS分别和G_general和G_example作差，G_general与MCS的差就是所有R_group原子，G_example与MCS的差就是Rp_group原子。所有差原子按照联通分量聚合后形成R_groups和Rp_groups。
   3. 根据连接点匹配R_group和Rp_group，即如果一个R_group和Rp_group与MCS有相同的连接点，则认为他们是一组对应关系。
   4. 考虑一个Rp_group可能对应多个R_group的情况（环化反应，即骨架上有两个R基团，而实施例中这两个R基团成环），这里首先尝试进行贪心匹配：对于每个Rp_group子图（具有一组附着点 S），寻找 **一组** R_groups子图，它们的附着点集合能“完全覆盖”这个 S（采用贪心算法），然后把这些选中的R_group子图合并成一个新的R_group（一个具有多个联通分量的R_group）。 
   5. 将R_groups和Rp_groups根据连接点进行一一匹配。
5. 将匹配好的R_groups和Rp_groups对代回到其他骨架。对于每个待代回骨架，在其中寻找是否有R_groups中的子结构，如有，使用对应的Rp_group进行替代