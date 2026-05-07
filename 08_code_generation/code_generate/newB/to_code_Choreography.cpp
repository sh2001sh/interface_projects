#include "to_code_Choreography.h" 
QVector<QString> destProtoList_41= { u8"W协议.W30_4",u8"W协议.W20_4"};
QVector<QString> templateList_41 = {u8"S01ToW304",u8"S106ToW204"};
QVector<bool> statusList41 = {false,false};
QVector<QString> src_list_41 = {u8"S协议.S0_1",u8"S协议.S106"};
QVector<qulonglong> src_receive_time_list_41 ={200,200};
QVector<QVector<int> > target_send_martix_41 ={{-1,-2},{-2,-1}};
qulonglong  code_test::getDstMsg_41(QString name){
int pos = -1;
for(int i=0;i< destProtoList_41.size();i++){
QString destProto = destProtoList_41[i];
if(destProto==name){
pos = i;break;
}
}
if(pos==-1) return pos;
return src_receive_time_list_41[pos];
}
qulonglong  code_test::getSrcTime_41(QString s1,QString s2){
int s1_pos=-1;
int s2_pos=-1;
for(int i=0;i< src_list_41.size();i++){
if(s1==src_list_41[i]) s1_pos=i;
if(s2==src_list_41[i]) s2_pos=i;
}
if(s1_pos==-1||s2_pos==-1) return -1;
return target_send_martix_41[s1_pos][s2_pos];
}
QMap<QString,uint>  code_test:: getAllSrcTime_41(){
QMap<QString,uint> res;
for(int i=0;i< templateList_41.size();i++){
res[templateList_41[i] ] = src_receive_time_list_41[i];
}
return res;
}
QMap<QString,uint>  code_test:: getAllDstTime_41(){
QMap<QString,uint> res;
for(int i=0;i< src_list_41.size();i++){
for(int j=0;j< src_list_41.size();j++)
res[src_list_41[i]+":"+src_list_41[j] ] = target_send_martix_41[i][j];
}
return res;
}
int code_test:: getStatus_41(QString s1){
int s1_pos=-1;
for(int i=0;i< destProtoList_41.size();i++){
if(s1==destProtoList_41[i]) s1_pos=i;
}
if(s1_pos==-1) return -1;
return statusList41[s1_pos];
}

